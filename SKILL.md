---
name: gtm-tag-request-form
category: skill
env: ["cc", "claude.ai"]
priority: on-demand
depends_on: {"connectors": ["supabase", "github"]}
triggers: ["GTMフォーム", "タグ設置依頼", "LP更新", "gtm-tag-request"]
outputs_to: ["github", "supabase"]
domain: ad
---

# GTM タグ設置依頼フォーム

GTM-59HCJW（cp.s-herb.com）のタグ設置をチームメンバーが依頼するためのWebフォーム。
送信するとChatWorkマイチャットに自動通知される。

## リポジトリ

- **GitHub**: `trngkb2026/gtm-tag-request-form`
- **公開URL**: https://trngkb2026.github.io/gtm-tag-request-form/
- **GitHub Pages**: main ブランチから自動デプロイ

## ファイル構成

| ファイル | 用途 |
|---|---|
| `index.html` | フォーム本体（HTML/CSS/JS 単一ファイル） |
| `campaign_scraper_requests.py` | urerud2c.jp からLP一覧を取得（requests版、Playwright不要） |
| `campaign_scraper.py` | 旧Playwright版（非推奨） |
| `update_form_lps.py` | campaigns.json → index.html のLP_NAMES/LP_CAMPAIGN_MAP/LP_GROUPS を自動更新 → GitHub Push |
| `.github/workflows/weekly-lp-update.yml` | 毎週月曜AM7:00(JST) LP自動更新 GitHub Actions cron |

## フォーム構成（index.html）

フィールド順序:
1. 代理店 / 運用者（GTM APIから直近1ヶ月更新タグベースで動的取得）
2. 媒体（チェックボックス: Meta/LINE/Google/Yahoo等 13種+その他）
3. 対象LP（中分類グループピル23種 → 検索＆複数選択）
4. 発火ページ（LP TOP / 確認画面 / 再確認画面 / サンクスページ）
5. 設置プレビュー（対象URL自動生成 + 発火ページ別タグ名称）
6. 作業種別（既存タグへトリガー追加[デフォルト] / 新規タグ作成＋トリガー設定）
7. タグコード/スニペット（新規タグ選択時のみ表示）
8. 設置希望日
9. 依頼者（宇留野/小松崎/蓮井/龍山）
10. 依頼日（自動記録）

## JSデータ構造

### LP_NAMES
全LP名の配列（237件）。`update_form_lps.py` で週次更新。

### LP_CAMPAIGN_MAP
LP名 → ecforce campaign_id のマッピング（238件）。
- LP TOP URL: `https://cp.s-herb.com/{LP名}`
- 確認画面URL: `https://cp.s-herb.com/orders/{campaign_id}/confirm`
- 再確認画面URL: `https://cp.s-herb.com/orders/{campaign_id}/reconfirm`
- サンクスURL: `https://cp.s-herb.com/orders/thanks/{campaign_id}/landing`

ソース: `TEN-Claude-Code/taro-tools/scripts/gtm_bulk_create_triggers_97.py` のCAMPAIGNS変数。

### LP_GROUPS
23グループの中分類:
PKG1J(59) / PKG1N(17) / PKG1G(10) / PKG1L(5) / PKG1Q(7) / PKG1R(7) / PKG1KS(3) / PKG1_IC(3) / PKG1他(8) / PKG3C(16) / PKG3E(9) / PKG3D(2) / PKG3K(5) / PC1A(22) / PC15D(7) / PCPKG(7) / PK22H(3) / PK22J(3) / PK22L(5) / MPC1A(5) / MPK(10) / PLTP(3) / その他(21)

## Supabase Edge Functions

プロジェクト: `hiuclxudffbdtqtzlirc`

### gtm-chatwork-notify (v10)
- **URL**: `https://hiuclxudffbdtqtzlirc.supabase.co/functions/v1/gtm-chatwork-notify`
- **方式**: POST, verify_jwt=false
- **機能**: フォーム送信データを受け取り、ChatWorkマイチャット（room:22301916）に依頼メッセージを送信
- **送信内容**:
  - [info]ブロック: 依頼者/代理店/媒体/対象LP/発火ページ/作業種別/希望日
  - 【タグ名称】[code]ブロック: 発火ページ別タグ名（例: N&M - SmartNews LP / N&M - SmartNews サンクス）
  - 【設置対象URL】[code]ブロック: campaign_idベースの実URL一覧
  - 【タグコード】[code]ブロック: 新規タグ作成時のみ
- **認証**: ChatWork APIトークンはSupabase Vault `chatwork_api_token` から取得

### gtm-recent-agencies (v5)
- **URL**: `https://hiuclxudffbdtqtzlirc.supabase.co/functions/v1/gtm-recent-agencies`
- **方式**: GET, verify_jwt=false, Cache-Control: max-age=3600
- **機能**: GTM API（サービスアカウント認証）でlive versionの全タグを取得し、直近1ヶ月以内に更新されたタグの代理店名を抽出して返す
- **認証**: GCPサービスアカウントキーはSupabase Vault `gtm_service_account_key` から取得
- **除外フィルタ**: GA4/SiTest/CATS/IM(/cp/sb/setCookies/AiDeal/HP/Wizaa/つくーる/Microsoft Clarity/ecforce
- **レスポンス例**: `{"agencies":["AA","N&M","星組",...], "totalTags":215, "recentCount":215, "source":"live v652"}`

## 週次LP更新パイプライン

### GitHub Actions ワークフロー
- **ファイル**: `.github/workflows/weekly-lp-update.yml`
- **スケジュール**: 毎週日曜 22:00 UTC（= 月曜 7:00 JST）
- **手動実行**: `workflow_dispatch` で即時実行可

### 実行フロー
```
1. campaign_scraper_requests.py scrape
   → urerud2c.jp にログイン（GitHub Secrets: URERUD2C_ACCOUNT_ID/LOGIN_ID/PASSWORD）
   → キャンペーン一覧をページネーション巡回
   → LP URL / campaign_id 抽出
   → output/campaigns.json

2. update_form_lps.py --input output/campaigns.json
   → campaigns.json を解析
   → LP_NAMES / LP_CAMPAIGN_MAP / LP_GROUPS / グループピルHTML を更新
   → index.html 書き換え → GitHub Push

3. GitHub Pages 自動デプロイ
```

### GitHub Secrets（trngkb2026/gtm-tag-request-form）
- `URERUD2C_ACCOUNT_ID`: Supabase Vault `urerud2c_account_id` と同値
- `URERUD2C_LOGIN_ID`: Supabase Vault `urerud2c_login_id` と同値
- `URERUD2C_PASSWORD`: Supabase Vault `urerud2c_password` と同値

## GTMコンテナ情報

- **コンテナ**: GTM-59HCJW（cp.s-herb.com）
- **Account ID**: 80163
- **Container ID**: 905354
- **GCP SA**: gtm-api-claude@s-herb-gtm.iam.gserviceaccount.com
- **タグ命名規則**: `{代理店} - {媒体} {ページ種別}` (例: N&M - SmartNews LP)
- **TPLトリガー**:
  - ID 282: TPL - LPページ
  - ID 283: TPL - 確認・アップセルページURL
  - ID 284: TPL - 再確認ページURL
  - ID 285: TPL - 申込完了ページURL ランディング
  - ID 286: TPL - 申込完了ページURL アップセル
  - ID 316: TPL - 申込完了ページURL ランディング | アップセル

## CC向けタスク

### LP更新を手動実行する場合
```bash
cd ~/path/to/gtm-tag-request-form
python campaign_scraper_requests.py scrape \
  --account-id "$(supabase secrets get urerud2c_account_id)" \
  --login-id "$(supabase secrets get urerud2c_login_id)" \
  --password "$(supabase secrets get urerud2c_password)"

GITHUB_TOKEN="$(supabase secrets get github_pat)" \
  python update_form_lps.py --input output/campaigns.json
```

### GitHub Issue
- TEN-Claude-Code/taro-tools#129: GTMフォーム: LP名リスト週次自動更新

## 関連スキル
- `gtm-builder`: GTMタグ構成案生成・インポートJSON作成
- `container_profile.md`: GTM-59HCJWの全タグ/トリガー/変数プロファイル（~/.claude/skills/gtm-builder/references/）
