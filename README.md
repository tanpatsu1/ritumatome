# 立命館 STUDENT PORTAL お知らせ自動要約システム

## セットアップ(初回のみ)
```bash
pip install -r requirements.txt
playwright install chromium
```
Gemini APIキーを環境変数に設定:
- Windows (PowerShell): `setx GEMINI_API_KEY "あなたのキー"` → ターミナル再起動
- Mac: `echo 'export GEMINI_API_KEY="あなたのキー"' >> ~/.zshrc && source ~/.zshrc`

## 初回ログイン
```bash
python main.py --login
```
ブラウザが開くのでRAINBOW IDでログイン → ターミナルでEnter。セッションは browser_profile/ に保存され再ログイン不要。

## 実行
```bash
python main.py
```
- 初回: 溜まった全件を処理(バックログモード)。途中で止まっても再実行で続きから再開
- 2回目以降: 新着差分のみ処理(数十秒)
- 出力: `archive.html`(全件アーカイブ) / `digest.html`(新着ダイジェスト)

## 毎朝自動実行
- Windows: タスクスケジューラで `python main.py --headless` を毎朝7:00に登録
- Mac: `crontab -e` → `0 7 * * * cd /path/to/ritsumei_portal && python3 main.py --headless`

## 注意
- 既読状態はブラウザの localStorage に保存(同じPCの同じブラウザで開くこと)
- セッションは数週間で切れることがある → `--login` で再ログイン
- アクセスは1日1回・人間と同等速度。大学の利用規約の範囲内で自己責任で運用すること
