# GTM Tag Setup Request Form

GTMタグ設置依頼フォーム — ChatWork通知付き

## 概要
GTMタグの設置・変更依頼を標準化するWebフォーム。
フォーム送信時にChatWorkへ自動通知を行う。

## 構成
- `index.html` — フォームUI（検索可能なLP選択、タグ名自動生成、バリデーション）
- `api/notify.js` — ChatWork通知API（Vercel Serverless Function）
- `vercel.json` — Vercelデプロイ設定

## デプロイ先
- **フォーム**: Supabase Edge Function `gtm-form`
- **通知API**: Supabase Edge Function `gtm-chatwork-notify`
- URL: `https://hiuclxudffbdtqtzlirc.supabase.co/functions/v1/gtm-form`

## 機能
- 代理店・媒体・タグ種別の選択
- 237本のLP名から検索・複数選択
- タグ名の自動生成プレビュー（`[代理店] - [媒体] [種別サフィックス]`）
- 発火ページ種別の指定
- ChatWorkルーム(306672911)への自動通知

## 環境変数
- `CHATWORK_API_TOKEN` — ChatWork APIトークン（Supabase Vaultで管理）
