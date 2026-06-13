#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth_state.json 動作検証ツール(クラウド模擬)
============================================
browser_profile/ を一切使わず、auth_state.json のクッキーだけで、
ヘッドレス(画面なし)でポータルのお知らせ一覧を開けるか検証する。
これが成功すれば GitHub Actions でも動く見込みが高い。

使い方:
  python test_auth.py
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

DIR = Path(__file__).parent
AUTH_FILE = DIR / "auth_state.json"
LIST_URL = "https://sp.ritsumei.ac.jp/studentportal/s/information-home"


def main():
    if not AUTH_FILE.exists():
        print("!! auth_state.json がありません。先に `python export_auth.py` を実行してください。")
        return

    with sync_playwright() as p:
        # 永続プロファイルは使わない。クッキーだけを読み込む(=クラウドと同じ条件)
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=str(AUTH_FILE),
            viewport={"width": 1440, "height": 1000},
            locale="ja-JP",
        )
        page = ctx.new_page()

        print("[1] ヘッドレスで一覧ページへアクセス...")
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        url = page.url.lower()
        if any(k in url for k in ("login", "auth", "sso", "idp")):
            print("!! 失敗: ログインページに飛ばされました。")
            print(f"   現在のURL: {page.url}")
            print("   → クッキー方式ではセッションが維持できない可能性。方針転換を検討。")
            browser.close()
            return

        # 一覧テーブルが描画されるか(=ログイン済みでデータが見えているか)を確認
        try:
            page.wait_for_selector("c-r_-custom-datatable-cmp table tbody tr", timeout=30000)
            rows = page.locator("c-r_-custom-datatable-cmp table tbody tr").count()
            print(f"[OK] 成功! お知らせ一覧が表示されました(行数: {rows})")
            print("    → クラウド(GitHub Actions)でも動く見込みが高いです。Step 2へ進めます。")
        except Exception:
            print("!! お知らせテーブルが見つかりませんでした。")
            print(f"   現在のURL: {page.url}")
            shot = DIR / "test_auth_screenshot.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"   画面を {shot.name} に保存しました。これをClaudeに送ってください。")

        browser.close()


if __name__ == "__main__":
    main()
