# じゃらんクーポン監視Bot

じゃらんの公開クーポン一覧を低頻度で確認し、条件の良い**新着クーポン**をDiscordへ通知します。

このBotは、ログイン、クーポンの自動取得、予約操作を行いません。通知後、公式ページで利用条件を確認して手動で取得してください。

## 主な機能

- 全国の宿泊施設クーポンを指定検索条件で最終ページまで監視
- 毎回の全件スナップショットから追加・内容変更・掲載終了を判定
- 「クーポン額 ÷ 最低予約金額」が50%を超える新着だけ通知
- 初回は既存クーポンを通知せず、監視対象として記録
- クーポンIDと宿番号による重複通知防止
- 新着通知がなかった日は21時以降にDiscordへ正常稼働を日次報告
- Discord Embed通知
- Python標準ライブラリのみで動作
- じゃらん側のHTML変更で0件になった場合は異常終了し、誤って状態を更新しない

既定値は「実質割引率が50%を超える」クーポンだけです。50%ちょうどは通知対象に含みません。

## 1. セットアップ

Python 3.10以上を用意します。

```bash
cp config.example.json config.json
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
```

Webhook URLはDiscordの「サーバー設定 → 連携サービス → ウェブフック」から作成できます。URLは秘密情報なので、`config.json`へ書かず環境変数で設定する方法を推奨します。

## 2. 動作確認

現在の掲載内容を取得し、条件に合うものをターミナルへ表示します。通知も状態保存も行いません。

```bash
python3 jalan_coupon_bot.py --config config.json --dry-run
```

通常は一覧の総件数から最終ページを自動計算します。短時間の確認で先頭1ページだけ取得する場合は `--pages 1` を追加できます。

Discord接続だけをテストします。

```bash
python3 jalan_coupon_bot.py --config config.json --test-webhook
```

## 3. 初回実行

```bash
python3 jalan_coupon_bot.py --config config.json
```

初回は既存クーポンを `data/state.json` に記録し、通知しません。2回目以降、新しいクーポンだけが判定対象です。初回から条件一致分を通知したい場合だけ、次を使います。

```bash
python3 jalan_coupon_bot.py --config config.json --notify-existing
```

## 4. 12分おきに実行（cron）

プロジェクトの絶対パスが `/opt/jalan-coupon-watcher` の例です。`crontab -e` に追加します。

```cron
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
5,17,29,41,53 * * * * cd /opt/jalan-coupon-watcher && /usr/bin/python3 jalan_coupon_bot.py --config config.json >> bot.log 2>&1
```

Webhook URLをcrontabへ直接書きたくない場合は、権限を絞った環境ファイルやsystemd timerを使ってください。

## GitHub Actionsで常時実行

`.github/workflows/monitor.yml` を同梱しています。ZIPを展開した中身をGitHubリポジトリ直下へアップロードすると、毎時5分・17分・29分・41分・53分（約12分間隔）を目安に自動実行できます。

1. GitHubで新しいリポジトリを作成します。
2. ZIPを展開し、中身をリポジトリ直下へアップロードします。
3. **Settings → Secrets and variables → Actions → New repository secret** を開きます。
4. 名前を `DISCORD_WEBHOOK_URL`、値をDiscordのWebhook URLにして保存します。
5. **Actions → Monitor Jalan coupons → Run workflow** から初回実行します。

初回は現在のクーポンを記録するだけで通知しません。初回から通知したい場合は、手動実行画面で「初回から現在の該当クーポンを通知する」を有効にします。

日次報告をすぐ試す場合は、手動実行画面で「新着なしの日次報告を今すぐ送る」を有効にします。

差分判定用の全件スナップショット `data/state.json` は、変更があったときだけActionsが自動コミットします。リポジトリ設定でActionsの書き込みが禁止されている場合は、**Settings → Actions → General → Workflow permissions** を **Read and write permissions** に変更してください。

GitHubの定期実行は混雑時に遅れる場合があります。また、非公開リポジトリではActionsの月間無料枠を消費します。新着通知が一度もなかった日は、日本時間21時以降の最初の正常監視で「新しい該当クーポンなし」という日次報告を送ります。

## 設定

`config.json` の主な項目です。

| 項目 | 既定値 | 意味 |
|---|---:|---|
| `minimum_discount_rate` | `0.5` | この値を超える実質割引率だけ通知（50%ちょうどは対象外） |
| `anomaly_discount_rate` | `0.5` | 高割引率として強調する基準（50%） |
| `match_mode` | `rate` | 割引率だけで通知判定 |
| `pages_to_scan` | `0` | `0` は総件数から最終ページまで自動走査 |
| `max_pages_to_scan` | `100` | 異常時の安全上限。必要ページ数が超えた場合は状態を更新せず終了 |
| `daily_status_hour_jst` | `21` | 新着がなかった日の日次報告を開始する日本時間の時刻 |
| `request_delay_seconds` | `2.0` | ページ間の待機秒数（最低1秒） |
| `request_retry_count` | `3` | 空応答・通信エラー時のページ再試行回数 |
| `retry_delay_seconds` | `3.0` | 再試行までの待機秒数 |
| `write_run_metadata` | `false` | 実行時刻も状態へ保存するか。GitHubでは不要なコミットを避けるためfalse推奨 |
| `state_file` | `data/state.json` | 重複通知防止データ |

検索条件は各ページで維持され、ページ取得が1つでも完了しない場合は通知や状態更新を行いません。

## 注意

- 利用条件・配布状況は必ず通知先のじゃらん公式ページで確認してください。
- サイト構造や規約が変わった場合は利用を止め、コードと設定を見直してください。
- `data/state.json` を削除すると初回状態に戻ります。
- `--notify-existing` を付けて初回通知が失敗した場合は状態を保存しないため、再実行できます。
