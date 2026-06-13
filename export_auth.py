#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ログインセッション書き出しツール
================================
今ログイン済みの browser_profile/ から、クッキー等を auth_state.json に書き出す。
この auth_state.json を GitHub Secrets に登録すれば、クラウド側でログイン状態を
再現できる(はず)。

使い方:
  python export_auth.py
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

DIR = Path(__file__).parent
USER_DATA_DIR = DIR / "browser_profile"
AUTH_OUT = DIR / "auth_state.json"
LIST_URL = "https://sp.ritsumei.ac.jp/studentportal/s/information-home"


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=False,        # 画面を見せて、本当にログイン済みか確認できるように
            viewport={"width": 1440, "height": 1000},
            locale="ja-JP",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("[1] ポータルへアクセスしてログイン状態を確認します...")
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        url = page.url.lower()
        if any(k in url for k in ("login", "auth", "sso", "idp")):
            print("!! ログインページに飛ばされました。先に `python main3.py --login` で")
            print("   ログインを完了させてから、もう一度このスクリプトを実行してください。")
            ctx.close()
            return

        print("[2] ログイン状態を確認。クッキー等を書き出します...")
        ctx.storage_state(path=str(AUTH_OUT))
        print(f"[OK] {AUTH_OUT.name} を書き出しました。")
        print("    次は test_auth.py で、このファイルだけで動くか検証します。")
        ctx.close()


if __name__ == "__main__":
    main()
