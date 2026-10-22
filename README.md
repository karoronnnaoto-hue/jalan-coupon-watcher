# じゃらんクーポン監視Bot

じゃらんの公開クーポン一覧を低頻度で確認し、条件の良い**新着クーポン**をDiscordへ通知します。

このBotは、ログイン、クーポンの自動取得、予約操作を行いません。通知後、公式ページで利用条件を確認して手動で取得してください。

## 主な機能

- 全国の宿泊施設クーポンを指定検索条件で最終ページまで監視
- 公式の「ふるさとキャンペーン」から配布中の地域クーポンも監視
- 全ページを2巡して結果を統合し、ページ送り中の並び替えによる取りこぼしを軽減
- 毎回の全件スナップショットから追加・内容変更・掲載終了を判定
- 一時的に見えないクーポンは3回連続で欠落するまで掲載終了にしない
- 「クーポン額 ÷ 最低予約金額」が50%を超える新着だけ通知
- 初回は既存クーポンを通知せず、監視対象として記録
- クーポンIDと宿番号による重複通知防止
- 新着通知がなかった日は21時以降にDiscordへ正常稼働を日次報告
- Discord Embed通知
- Python標準ライブラリのみで動作
- じゃらん側のHTML変更で0件になった場合は異常終了し、誤って状態を更新しない

## ホテル料金の異常値監視

`jalan_hotel_price_bot.py` は全国の宿の表示最低料金を約3時間ごとに確認し、同じ宿の直前30日間に記録した最低価格より60%以上安い料金を検知した時点でDiscordへ通知します。通知は60-69%、70-79%、80-89%、90%以上の4段階に分類します。プラン名の「OFF」表記や通常価格の推定は使いません。

- 比較条件は毎日同じ「日付未定・1泊・1室・大人2名・料金が安い順」
- 全国の地域一覧を約100個の収集Botに均等分配して並列走査
- 100個すべてが成功した場合だけ、1個の集約Botが履歴を更新して通知
- 1回でも過去価格を記録できた宿は、次の走査から比較対象
- 現在料金が直前30日の記録済み最低価格より60%以上下落したら、到達した60/70/80/90%段階で即時通知
- 広告枠を除外し、検索結果件数と通常宿カード数が一致しなければ異常終了
- 約3時間ごとに、取得宿数・比較対象宿数・60/70/80/90%各段階の下落件数・新規通知件数をDiscordへ送信

東京都だけをローカル収集する場合:

```bash
python3 jalan_hotel_price_bot.py collect --config config.example.json --shard-index 0 --shard-count 100 --prefecture 130000 --output data/shards/tokyo.json
```

保存済みの全宿泊施設をExcel対応CSVへ書き出す場合:

```bash
python3 jalan_hotel_price_bot.py export --config config.example.json --output data/hotel_facilities.csv
```

GitHub Actionsの `Monitor Jalan hotel price anomalies` は約3時間ごと（毎日0:17、3:17、6:17、9:17、12:17、15:17、18:17、21:17ごろ・日本時間）に起動します。100ジョブへの分担後、実際に同時実行される数はGitHubアカウントの上限に従います。

既定値は「実質割引率が50%を超える」クーポンだけです。50%ちょうどは通知対象に含みません。

## 1. セットアップ

Python 3.10以上を用意します。



## 2. 動作確認

現在の掲載内容を取得し、条件に合うものをターミナルへ表示します。通知も状態保存も行いません。

```bash
python3 jalan_coupon_bot.py --config config.json --dry-run
```

通常は一覧の総件数と、取得終了分を含む全カード数から最終ページを自動計算します。短時間の確認で先頭1ページだけ取得する場合は `--pages 1` を追加できます。

Discord接続だけをテストします。

```bash
python3 jalan_coupon_bot.py --config config.json --test-webhook
```

## 3. 初回実行

```bash
python3 jalan_coupon_bot.py --config config.json
```

初回は既存クーポンを `data/state.json` に記録し、通知しません。2回目以降、新しいクーポンだけが判定対象です。現在掲載中の条件一致分を通知済みも含めてすべて再通知する場合は、次を使います。

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

初回は現在のクーポンを記録するだけで通知しません。現在の対象を通知済みも含めて再通知したい場合は、手動実行画面で「現在の該当クーポンをすべて通知する（通知済みを含む）」を有効にします。

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
| `scan_passes` | `2` | 全ページ走査の回数（最大3）。各巡回の結果を統合して取りこぼしを軽減 |
| `missing_grace_runs` | `3` | この回数連続で見つからなかった後に掲載終了と判定 |
| `include_regional_coupons` | `true` | ご当地・地域クーポンも監視対象に含める |
| `max_regional_campaigns` | `100` | ご当地キャンペーン数の安全上限 |
| `max_regional_detail_pages` | `200` | 地域クーポン詳細ページ数の安全上限 |
| `regional_request_delay_seconds` | `1.0` | ご当地キャンペーンページ間の待機秒数（最低0.5秒） |
| `hotel_price_drop_rates` | `[0.6, 0.7, 0.8, 0.9]` | 記録済み最低価格からの通知段階（60/70/80/90%以上） |
| `hotel_price_history_days` | `30` | 記録済み最低価格を保持する日数 |
| `hotel_price_parallel_shards` | `100` | 全国の地域を分担する収集Bot数 |
| `hotel_price_request_delay_seconds` | `1.0` | 地域検索ページ間の待機秒数（最低0.5秒） |
| `hotel_price_max_regions_per_prefecture` | `40` | 1都道府県の地域数に対する安全上限 |
| `hotel_price_max_pages_per_region` | `50` | 1地域の検索ページ数に対する安全上限 |
| `hotel_price_max_pages_per_shard` | `100` | 1収集Botの総ページ数に対する安全上限 |
| `hotel_price_state_file` | `data/hotel_price_state.json.gz` | gzip圧縮した日次料金履歴と通知済み情報 |
| `daily_status_hour_jst` | `21` | 新着がなかった日の日次報告を開始する日本時間の時刻 |
| `request_delay_seconds` | `2.0` | ページ間の待機秒数（最低1秒） |
| `request_retry_count` | `3` | 空応答・通信エラー時のページ再試行回数 |
| `retry_delay_seconds` | `3.0` | 再試行までの待機秒数 |
| `write_run_metadata` | `false` | 実行時刻も状態へ保存するか。GitHubでは不要なコミットを避けるためfalse推奨 |
| `state_file` | `data/state.json` | 重複通知防止データ |

宿クーポンの検索条件は各ページで維持されます。地域クーポンは公式の「ふるさとキャンペーン」公開データでクーポンありと示された特集をたどり、配布終了表示のないクーポンだけを統合します。必要なページ取得が1つでも完了しない場合は通知や状態更新を行いません。

## 注意

- 利用条件・配布状況は必ず通知先のじゃらん公式ページで確認してください。
- サイト構造や規約が変わった場合は利用を止め、コードと設定を見直してください。
- `data/state.json` を削除すると初回状態に戻ります。
- `--notify-existing` を付けた通知が失敗した場合は状態を保存しないため、再実行できます。
