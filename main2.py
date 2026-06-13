#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
立命館 STUDENT PORTAL お知らせ自動要約システム
================================================
機能:
  1. Playwrightでお知らせ一覧を全件取得(初回バックログ対応・チェックポイント方式)
  2. 進路・就職/インターン/大学院は強制★★★(コード側で保証、AI判定に依存しない)
  3. Gemini APIで10件バッチ判定(★/カテゴリ/要約。★3は詳細要約)
  4. archive.html(全件・フィルタ・検索・既読管理) と digest.html(新着のみ) を生成

使い方:
  python main.py --login    # 初回のみ: ブラウザが開くので手動でログイン→Enter
  python main.py            # 通常実行(初回は全件バックログ処理、以降は差分のみ)
  python main.py --headless # ログイン済みならヘッドレスで実行可

セットアップ:
  pip install playwright requests
  playwright install chromium
  環境変数 GEMINI_API_KEY を設定 (または下の CONFIG に直書き)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============================================================
# CONFIG
# ============================================================
BASE = "https://sp.ritsumei.ac.jp"
LIST_URL = BASE + "/studentportal/s/information-home"

DIR = Path(__file__).parent
USER_DATA_DIR = DIR / "browser_profile"   # ログインセッション保持
DATA_FILE = DIR / "notifications.json"    # 全データ(チェックポイント)
ARCHIVE_OUT = DIR / "archive.html"
DIGEST_OUT = DIR / "digest.html"
TEMPLATE = DIR / "archive_template.html"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # ここに直書きも可
GEMINI_MODEL = "gemini-2.5-flash"
BATCH_SIZE = 10          # 1リクエストあたりの判定件数
DETAIL_WAIT_SEC = 1.5    # 詳細ページ間の待機(サーバ負荷配慮)

USER_PROFILE = "立命館大学 理工学部 1回生(2030年卒予定)・BKC(びわこくさつキャンパス)所属。物理・数学・情報を学ぶ。"

# 強制★★★キーワード(タイトル or 本文 or 配信カテゴリに含まれたら無条件でキャリア扱い)
CAREER_KEYWORDS = [
    "進路・就職", "就職", "就活", "採用", "インターン", "キャリア",
    "大学院", "院試", "進学", "研究科", "合同説明会", "企業説明会", "ES対策",
]

# ============================================================
# 1. スクレイピング
# ============================================================

def is_login_page(page) -> bool:
    url = page.url.lower()
    return ("login" in url) or ("auth" in url) or ("sso" in url) or ("idp" in url)


def scrape_list(page) -> list:
    """お知らせ一覧から全行を取得して dict のリストで返す"""
    print("[list] 一覧ページへ移動...")
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_selector("c-r_-custom-datatable-cmp table tbody tr", timeout=30000)
    except PWTimeout:
        if is_login_page(page):
            print("!! ログインが必要です。`python main.py --login` を先に実行してください。")
            sys.exit(1)
        raise

    # 「すべて表示」をクリックして全件ロード
    try:
        btn = page.locator("button", has_text="すべて表示").first
        if btn.is_visible():
            print("[list] 「すべて表示」をクリック")
            btn.click()
            page.wait_for_timeout(3000)
    except Exception:
        pass

    # 行数が増えなくなるまでスクロール(無限スクロール/遅延ロード対策)
    prev, stable = -1, 0
    for _ in range(120):
        rows = page.locator("c-r_-custom-datatable-cmp table tbody tr").count()
        if rows == prev:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        prev = rows
        page.mouse.wheel(0, 4000)
        # datatable内スクロールにも対応
        page.evaluate("""() => {
            const sc = document.querySelector('c-r_-custom-datatable-cmp .slds-scrollable_y');
            if (sc) sc.scrollTop = sc.scrollHeight;
            window.scrollTo(0, document.body.scrollHeight);
        }""")
        page.wait_for_timeout(800)
    print(f"[list] 取得行数: {prev}")

    # 行データ抽出: LWCのShadow DOMはquerySelectorで越えられないため
    # レンダリング済みHTML文字列を取得して正規表現でパースする(これが最も確実)
    html = page.content()
    items = parse_list_html(html)
    print(f"[list] パース完了: {len(items)}件")

    if not items:
        debug = DIR / "debug_list.html"
        debug.write_text(html, encoding="utf-8")
        print(f"!! 抽出0件。画面構造の確認用に {debug.name} を保存しました。"
              "このファイルをClaudeに送ってください。")
    return items


def parse_list_html(html: str) -> list:
    """一覧ページのHTML文字列から通知行を抽出する"""
    out, seen = [], set()
    # 各行は slds-hint-parent クラスの <tr> で始まる
    blocks = re.split(r'<tr [^>]*slds-hint-parent', html)[1:]

    def cell(block, label):
        # まず data-cell-value(機械可読値)を優先
        m = re.search(r'data-label="' + re.escape(label) + r'"[^>]*data-cell-value="([^"]*)"', block)
        if m:
            return _unescape(m.group(1)).strip()
        # 無ければセル内テキストをタグ除去で
        m = re.search(r'data-label="' + re.escape(label) + r'"(.*?)</t[dh]>', block, re.S)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()
        return ""

    for b in blocks:
        url_raw = cell(b, "タイトル")  # data-cell-value はURL
        if "r-information" not in url_raw:
            continue
        m = re.search(r'r-information/([A-Za-z0-9]+)', url_raw)
        nid = m.group(1) if m else url_raw
        if nid in seen:
            continue
        seen.add(nid)
        # タイトル文字列は <a ... title="...">
        mt = re.search(r'r-information/[A-Za-z0-9]+/view"[^>]*?title="([^"]*)"', b)
        title = _unescape(mt.group(1)).strip() if mt else ""
        if not title:  # title属性が無ければリンクテキスト
            mt = re.search(r'r-information/[A-Za-z0-9]+/view"[^>]*>([^<]+)</a>', b)
            title = _unescape(mt.group(1)).strip() if mt else "(無題)"
        out.append({
            "id": nid,
            "url": "https://sp.ritsumei.ac.jp" + url_raw if url_raw.startswith("/") else url_raw,
            "ttl": title,
            "published": cell(b, "公開日"),
            "end": cell(b, "終了日"),
            "dept": cell(b, "担当部課"),
            "portal_cat": cell(b, "配信カテゴリ"),
            "deadline_raw": cell(b, "期日設定"),
            "kubun": cell(b, "お知らせ区分"),
        })
    return out


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))


def scrape_detail(page, url: str) -> str:
    """通知詳細ページから本文テキストを抽出。
    LWCのShadow DOM対策でHTML文字列をパースする。"""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2800)  # LWCレンダリング待ち
    html = page.content()
    return extract_body_from_html(html)


def extract_body_from_html(html: str) -> str:
    """詳細ページHTMLから本文テキストを抽出(多段フォールバック)"""
    def strip_tags(s):
        s = re.sub(r'<br\s*/?>', '\n', s)
        s = re.sub(r'</(p|div|li|tr|h[1-6])>', '\n', s)
        s = re.sub(r'<[^>]+>', '', s)
        return _unescape(s)

    # 1) リッチテキスト出力(Salesforceの本文の定番)
    chunks = re.findall(
        r'<lightning-formatted-rich-text[^>]*>(.*?)</lightning-formatted-rich-text>',
        html, re.S)
    chunks += re.findall(
        r'class="slds-rich-text-editor__output"[^>]*>(.*?)</', html, re.S)
    texts = [strip_tags(c).strip() for c in chunks]
    texts = [t for t in texts if len(t) > 30]
    if texts:
        # 最長のものを本文とみなす(重複除去)
        seen, picked = set(), []
        for t in sorted(texts, key=len, reverse=True):
            if t[:40] not in seen:
                seen.add(t[:40]); picked.append(t)
        return "\n\n".join(picked)[:8000]

    # 2) record詳細コンテナ
    m = re.search(r'forceCommunityRecordDetail(.*?)</div>\s*</div>', html, re.S)
    if m:
        t = strip_tags(m.group(1)).strip()
        if len(t) > 50:
            return t[:8000]

    # 3) 最終手段: body全体からタグ除去(ヘッダ等のノイズ込み)
    mb = re.search(r'<div role="main"[^>]*>(.*?)</div>\s*<footer', html, re.S)
    src = mb.group(1) if mb else html
    return strip_tags(src).strip()[:8000]


# ============================================================
# 2. 強制ルール + Gemini バッチ判定
# ============================================================

def apply_forced_rules(it: dict):
    """キャリア・進学系は AI を介さずコードで★3を保証"""
    hay = f"{it.get('ttl','')} {it.get('portal_cat','')} {it.get('body','')[:500]}"
    it["career"] = any(kw in hay for kw in CAREER_KEYWORDS)
    if it["career"]:
        it["star"] = 3


def gemini_classify(batch: list) -> dict:
    """10件まとめてJSONで判定。戻り値: {id: {star, cat, summary}}"""
    items_text = "\n\n".join(
        f"---\nID: {it['id']}\nタイトル: {it['ttl']}\n配信カテゴリ: {it['portal_cat']}\n"
        f"担当部課: {it['dept']}\n期日: {it.get('deadline_raw') or 'なし'}\n"
        f"強制★3対象(キャリア・進学): {'はい' if it.get('career') else 'いいえ'}\n"
        f"本文:\n{it.get('body','')[:2500]}"
        for it in batch
    )
    prompt = f"""あなたは大学のお知らせを学生向けに仕分けるアシスタントです。
対象学生: {USER_PROFILE}

以下の{len(batch)}件の通知を判定し、JSON配列のみを出力してください(説明文・コードブロック記法は禁止)。

各要素の形式:
{{"id": "通知ID", "star": 1|2|3, "cat": "就活・院|履修・成績|休講・補講|手続き・締切|留学|学生生活|その他", "summary": "要約"}}

判定基準:
- star3: 締切があり本人に該当する手続き、奨学金・授業料等の金銭、休講・補講、成績・履修の重要連絡、キャリア・進学系
- star2: 期限はあるが影響が中程度、把握しておくべき情報
- star1: 一般的な案内・イベント告知・広報
- 「強制★3対象: はい」の通知は必ず star=3、cat="就活・院"
- 衣笠・OICキャンパス限定でBKC学生に無関係なものは star を1段階下げる
- summary: star3は重要条件(日時・場所・締切・対象・申込方法)を含む3〜4文。star1-2は1文。
  この学生(理工1回生・BKC)にとっての関連性が高い場合はそれにも触れる。

通知一覧:
{items_text}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, timeout=120)
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  [gemini] レート制限(429)。{wait}秒待機して再試行 {attempt+1}/5")
                time.sleep(wait)
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
            arr = json.loads(text)
            return {str(o["id"]): o for o in arr}
        except Exception as e:
            print(f"  [gemini] リトライ {attempt+1}/5: {e}")
            time.sleep(8 * (attempt + 1))
    return {}


# ============================================================
# 3. ユーティリティ / 状態管理
# ============================================================

def load_state() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"items": {}}


def save_state(state: dict):
    DATA_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def norm_date(s: str) -> str:
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


# ============================================================
# 4. HTML生成
# ============================================================

def build_archive(state: dict):
    items = []
    for it in state["items"].values():
        items.append({
            "id": it["id"], "url": it["url"], "ttl": it["ttl"],
            "date": norm_date(it.get("published")) or "1970-01-01",
            "star": it.get("star", 1),
            "cat": it.get("cat") or it.get("portal_cat") or "その他",
            "career": it.get("career", False),
            "deadline": norm_date(it.get("deadline_raw")) or None,
            "dept": it.get("dept", ""),
            "sum": it.get("summary", ""),
            "body": it.get("body", "")[:4000],
        })
    tpl = TEMPLATE.read_text(encoding="utf-8")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")  # </script>対策
    html = tpl.replace("/*__DATA__*/[]", data_json) \
              .replace("__UPDATED__", now) \
              .replace("__TODAY__", date.today().isoformat())
    ARCHIVE_OUT.write_text(html, encoding="utf-8")
    print(f"[out] archive.html 生成 ({len(items)}件)")


def build_digest(new_items: list):
    if not new_items:
        return
    new_items = sorted(new_items, key=lambda x: (-x.get("star", 1), x.get("deadline_raw") or "9999"))
    rows = ""
    for it in new_items:
        star = it.get("star", 1)
        color = {3: "#9e2235", 2: "#b07b1e", 1: "#999"}[star]
        dl = f"<span style='color:#b07b1e;font-size:12px'> | 期日 {it['deadline_raw']}</span>" if it.get("deadline_raw") else ""
        rows += f"""<div style="border-left:3px solid {color};background:#fff;border:1px solid #e5e2da;
            border-left:4px solid {color};border-radius:10px;padding:14px 16px;margin-bottom:10px">
          <div style="font-size:11px;color:{color};letter-spacing:2px">{'★'*star}
            <span style="color:#888;margin-left:8px">{it.get('cat') or it.get('portal_cat','')}</span>{dl}</div>
          <div style="font-weight:700;font-size:15px;margin:4px 0">{it['ttl']}</div>
          <div style="font-size:13px;color:#555">{it.get('summary','')}</div>
          <a href="{it['url']}" style="font-size:12px;color:#9e2235">ポータルで開く →</a>
        </div>"""
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ポータル新着ダイジェスト</title></head>
<body style="background:#f6f5f1;font-family:'Hiragino Sans','Noto Sans JP',sans-serif;padding:24px;max-width:680px;margin:0 auto">
<h2 style="font-size:18px">📬 ポータル新着 {len(new_items)}件 — {datetime.now().strftime('%m/%d %H:%M')}</h2>
{rows}
<p style="font-size:11px;color:#999">全件は archive.html で確認できます。</p>
</body></html>"""
    DIGEST_OUT.write_text(html, encoding="utf-8")
    print(f"[out] digest.html 生成 ({len(new_items)}件)")


# ============================================================
# MAIN
# ============================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="初回ログイン(手動)")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if not args.login and not GEMINI_API_KEY:
        print("!! 環境変数 GEMINI_API_KEY が未設定です")
        sys.exit(1)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=args.headless and not args.login,
            viewport={"width": 1440, "height": 1000},
            locale="ja-JP",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.login:
            page.goto(LIST_URL)
            input(">> ブラウザでログインを完了したら Enter を押してください...")
            print("セッション保存完了。次回から `python main.py` で実行できます。")
            ctx.close()
            return

        state = load_state()

        # --- 一覧取得・差分検出 ---
        listed = scrape_list(page)
        new_ids = []
        for it in listed:
            if it["id"] not in state["items"]:
                state["items"][it["id"]] = it
                new_ids.append(it["id"])
            else:
                # 既存にも期日等の更新を反映
                state["items"][it["id"]].update(
                    {k: it[k] for k in ("deadline_raw", "end", "portal_cat") if it.get(k)})
        save_state(state)
        print(f"[diff] 新規 {len(new_ids)}件 / 既存 {len(listed)-len(new_ids)}件")

        # --- 本文取得(新しい順・1件ごとにチェックポイント保存) ---
        pending_body = [i for i in state["items"].values() if "body" not in i]
        pending_body.sort(key=lambda x: x.get("published", ""), reverse=True)
        print(f"[detail] 本文未取得 {len(pending_body)}件を処理")
        for n, it in enumerate(pending_body, 1):
            try:
                it["body"] = scrape_detail(page, it["url"])
                apply_forced_rules(it)
                print(f"  ({n}/{len(pending_body)}) {it['ttl'][:38]}")
            except Exception as e:
                print(f"  !! 失敗(次回再試行): {it['ttl'][:30]} : {e}")
            save_state(state)
            time.sleep(DETAIL_WAIT_SEC)

        ctx.close()

    # --- Gemini バッチ判定(未判定のみ・新しい順) ---
    pending_ai = [i for i in state["items"].values() if "summary" not in i and "body" in i]
    pending_ai.sort(key=lambda x: x.get("published", ""), reverse=True)
    print(f"[ai] 判定待ち {len(pending_ai)}件 → {-(-len(pending_ai)//BATCH_SIZE)}バッチ")
    for i in range(0, len(pending_ai), BATCH_SIZE):
        batch = pending_ai[i:i + BATCH_SIZE]
        res = gemini_classify(batch)
        for it in batch:
            r = res.get(str(it["id"]))
            if r:
                if not it.get("career"):           # 強制★3は上書きさせない
                    it["star"] = int(r.get("star", 2))
                    it["cat"] = r.get("cat", "その他")
                else:
                    it["star"], it["cat"] = 3, "就活・院"
                it["summary"] = r.get("summary", "")
        save_state(state)
        print(f"  バッチ {i//BATCH_SIZE + 1} 完了")
        time.sleep(6)  # 無料枠のレート制限を避けるため余裕をもって待機

    # --- 出力 ---
    build_archive(state)
    new_items = [state["items"][i] for i in new_ids if "summary" in state["items"].get(i, {})]
    build_digest(new_items)
    print("\n完了。archive.html をブラウザで開いてください。")


if __name__ == "__main__":
    main()
