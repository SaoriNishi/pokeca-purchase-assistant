# Pokeca Purchase Assistant

ポケモンカードの発売情報と在庫/抽選ページを監視し、購入チャンスを通知するローカルMVPです。

購入確定は手動確認にしています。CAPTCHA回避、購入制限回避、複数アカウント購入、店舗規約に反する自動購入は対象外です。

## できること

- 公式サイトから発売情報を取り込み、SQLiteに保存
- 店舗の商品ページや検索ページを監視
- 「在庫あり」「予約受付中」「抽選受付中」などの文言を検知
- Discord Webhookへ通知
- ローカル管理画面で発売情報、監視結果、通知履歴を確認

## セットアップ

```powershell
Copy-Item config.example.json config.json
```

`config.json` を編集します。

- `discord_webhook_url`: Discord通知を使う場合にWebhook URLを設定
- `stock_check_interval_minutes`: 在庫/抽選チェック間隔
- `retailers`: 監視したい店舗ページを追加

## 起動

このCodex環境ではバンドルPythonを使えます。

```powershell
& "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py init
& "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py serve
```

管理画面:

```text
http://127.0.0.1:8765
```

継続監視:

```powershell
& "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py watch
```

## コマンド

```powershell
& "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py import-releases
& "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py check-stock
```

## 店舗追加例

```json
{
  "name": "Example Shop",
  "url": "https://example.com/search?q=ポケモンカード",
  "product_keyword": "ポケモンカード",
  "enabled": true
}
```

`product_keyword` はページ本文に含まれるべき商品名です。新弾名が分かったら「アビスアイ」など具体的なキーワードにすると誤検知が減ります。

## 次に足すと便利な機能

- LINE Messaging API通知
- 店舗ごとの検知ルール
- 価格フィルター
- 抽選開始/終了日のカレンダー表示
- ブラウザで購入ページを自動オープン
