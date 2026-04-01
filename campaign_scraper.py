#!/usr/bin/env python3
"""
Campaign Scraper for urerud2c.jp
================================
管理画面にログインし、キャンペーン一覧→各詳細ページを巡回して
LP URL・アップセルURLを抽出しMarkdownに保存する。

Usage:
  # Step 1: 探索モード（DOM構造を確認）
  python campaign_scraper.py discover --account-id ACCT --login-id USER --password PASS

  # Step 2: 抽出モード（全キャンペーン巡回）
  python campaign_scraper.py scrape --account-id ACCT --login-id USER --password PASS

  # オプション
  --headed        ブラウザを表示して実行（デバッグ用）
  --output FILE   出力ファイル名（デフォルト: campaigns.md）
  --delay SEC     ページ間の待機秒数（デフォルト: 1.5）
  --max N         最大取得件数（テスト用）
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("❌ Playwright未インストール。以下を実行してください:")
    print("   pip install playwright && playwright install chromium")
    sys.exit(1)


# ============================================================
# 設定
# ============================================================
BASE_URL = "https://urerud2c.jp"
LOGIN_URL = f"{BASE_URL}/login"
CAMPAIGNS_URL = f"{BASE_URL}/admin/campaigns"
OUTPUT_DIR = Path("output")
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"


# ============================================================
# ユーティリティ
# ============================================================
def ensure_dirs():
    OUTPUT_DIR.mkdir(exist_ok=True)
    SCREENSHOTS_DIR.mkdir(exist_ok=True)


def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(text: str) -> str:
    return re.sub(r'[^\w\-]', '_', text)[:80]


# ============================================================
# ログイン
# ============================================================
async def login(page, account_id: str, login_id: str, password: str):
    """管理画面にログインする（3フィールド: アカウントID / ログインID / パスワード）"""
    print(f"🔐 ログイン中... {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    # スクリーンショット（デバッグ用）
    await page.screenshot(path=str(SCREENSHOTS_DIR / "01_login_page.png"), full_page=True)

    # まずページ内の全input要素を列挙してデバッグ出力
    all_inputs = await page.eval_on_selector_all(
        "input, select, textarea",
        """els => els.map(el => ({
            tag: el.tagName,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            placeholder: el.placeholder || '',
            label: el.labels?.[0]?.textContent?.trim() || ''
        }))"""
    )
    print(f"  📝 フォーム要素一覧 ({len(all_inputs)}件):")
    for inp in all_inputs:
        print(f"    <{inp['tag']} type='{inp['type']}' name='{inp['name']}' id='{inp['id']}' placeholder='{inp['placeholder']}' label='{inp['label']}'>")

    # 3フィールドのセレクタ候補
    account_selectors = [
        'input[name*="account"]',
        'input[id*="account"]',
        'input[placeholder*="アカウント"]',
    ]
    login_id_selectors = [
        'input[name*="login"]',
        'input[name*="email"]',
        'input[name*="username"]',
        'input[name*="user_id"]',
        'input[id*="login"]',
        'input[id*="email"]',
        'input[type="email"]',
        'input[placeholder*="ログイン"]',
        'input[placeholder*="メール"]',
    ]
    password_selectors = [
        'input[name*="password"]',
        'input[type="password"]',
    ]
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("ログイン")',
        'input[value="ログイン"]',
        'button:has-text("Login")',
    ]

    # フォールバック: text入力を順番に取得（アカウントID→ログインID→パスワード）
    text_inputs = await page.locator('input[type="text"], input[type="email"], input:not([type])').all()
    pass_inputs = await page.locator('input[type="password"]').all()

    async def find_field(selectors, label):
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1000):
                    print(f"  ✓ {label}発見: {sel}")
                    return el
            except Exception:
                continue
        return None

    # アカウントIDフィールド
    account_input = await find_field(account_selectors, "アカウントID欄")
    if not account_input and len(text_inputs) >= 2:
        account_input = text_inputs[0]
        print(f"  ✓ アカウントID欄（位置推定: 1番目のtext input）")

    # ログインIDフィールド
    login_input = await find_field(login_id_selectors, "ログインID欄")
    if not login_input and len(text_inputs) >= 2:
        login_input = text_inputs[1]
        print(f"  ✓ ログインID欄（位置推定: 2番目のtext input）")

    # パスワードフィールド
    pass_input = await find_field(password_selectors, "パスワード欄")
    if not pass_input and pass_inputs:
        pass_input = pass_inputs[0]
        print(f"  ✓ パスワード欄（位置推定: 1番目のpassword input）")

    if not all([account_input, login_input, pass_input]):
        html = await page.content()
        (OUTPUT_DIR / "login_page.html").write_text(html, encoding="utf-8")
        missing = []
        if not account_input: missing.append("アカウントID")
        if not login_input: missing.append("ログインID")
        if not pass_input: missing.append("パスワード")
        raise RuntimeError(f"ログインフォームの {', '.join(missing)} 欄が見つかりません。login_page.htmlを確認してください。")

    # 入力＆送信
    # fill() はJSイベントを発火しないためボタンのdisabledが解除されない。
    # click() + press_sequentially() で実キーイベントを発火させる。
    await account_input.click()
    await account_input.press_sequentially(account_id, delay=50)
    await login_input.click()
    await login_input.press_sequentially(login_id, delay=50)
    await pass_input.click()
    await pass_input.press_sequentially(password, delay=50)
    await page.wait_for_timeout(500)
    await page.screenshot(path=str(SCREENSHOTS_DIR / "02_login_filled.png"), full_page=True)

    # 送信ボタン（disabled解除後にクリック、またはEnterキーでフォーム送信）
    btn_clicked = False
    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                print(f"  ✓ 送信ボタン発見: {sel}")
                is_disabled = await btn.is_disabled()
                if is_disabled:
                    print(f"  ⚠️  ボタンがdisabled → JSでdisabled除去してクリック")
                    await page.evaluate("document.querySelector('button.login_btn, button[type=\"submit\"], input[type=\"submit\"]').removeAttribute('disabled')")
                await btn.click()
                btn_clicked = True
                break
        except Exception:
            continue
    if not btn_clicked:
        print("  ⚠️  ボタン未検出 → Enterキーでフォーム送信")
        await pass_input.press("Enter")

    # ログイン完了を待つ
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(2000)
    await page.screenshot(path=str(SCREENSHOTS_DIR / "03_after_login.png"), full_page=True)

    # ログイン成功確認
    current_url = page.url
    if "login" in current_url.lower():
        html = await page.content()
        (OUTPUT_DIR / "login_failed.html").write_text(html, encoding="utf-8")
        raise RuntimeError(f"ログインに失敗した可能性があります。URL: {current_url}")

    print(f"  ✅ ログイン成功！ → {current_url}")


# ============================================================
# 探索モード（discover）
# ============================================================
async def discover(page, account_id: str, login_id: str, password: str):
    """DOM構造を探索してスクショ＋HTMLを保存"""
    await login(page, account_id, login_id, password)

    print(f"\n📋 キャンペーン一覧を取得中... {CAMPAIGNS_URL}")
    await page.goto(CAMPAIGNS_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)
    await page.screenshot(path=str(SCREENSHOTS_DIR / "04_campaigns_list.png"), full_page=True)

    # HTML保存
    html = await page.content()
    (OUTPUT_DIR / "campaigns_list.html").write_text(html, encoding="utf-8")
    print(f"  💾 campaigns_list.html 保存完了")

    # テーブル/リスト内のリンクを探す
    links = await page.eval_on_selector_all(
        "a[href*='campaign']",
        "els => els.map(el => ({ href: el.href, text: el.textContent.trim().substring(0, 100) }))"
    )
    print(f"\n  🔗 キャンペーン関連リンク: {len(links)}件")
    for i, link in enumerate(links[:20]):
        print(f"    [{i}] {link['text'][:50]} → {link['href']}")

    # 最初のキャンペーン詳細ページへ移動してDOM構造確認
    campaign_links = [l for l in links if re.search(r'/campaigns/\d+', l['href'])]
    if campaign_links:
        first_url = campaign_links[0]['href']
        print(f"\n📄 最初のキャンペーン詳細を確認... {first_url}")
        await page.goto(first_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(SCREENSHOTS_DIR / "05_campaign_detail.png"), full_page=True)

        detail_html = await page.content()
        (OUTPUT_DIR / "campaign_detail_sample.html").write_text(detail_html, encoding="utf-8")
        print(f"  💾 campaign_detail_sample.html 保存完了")

        # URL風の文字列を全抽出
        urls_in_page = await page.eval_on_selector_all(
            "*",
            """els => {
                const urls = new Set();
                els.forEach(el => {
                    // href属性
                    if (el.href && el.href.startsWith('http')) urls.add(el.href);
                    // value属性（inputフィールド）
                    if (el.value && el.value.startsWith('http')) urls.add(el.value);
                    // テキスト中のURL
                    const text = el.textContent || '';
                    const matches = text.match(/https?:\\/\\/[^\\s<>"]+/g);
                    if (matches) matches.forEach(m => urls.add(m));
                });
                return [...urls];
            }"""
        )
        print(f"\n  🌐 ページ内URL一覧 ({len(urls_in_page)}件):")
        for url in urls_in_page:
            print(f"    {url}")

        # LP/アップセルに関連しそうなラベルを探す
        labels = await page.eval_on_selector_all(
            "label, th, dt, .label, [class*='label'], [class*='title']",
            "els => els.map(el => ({ tag: el.tagName, text: el.textContent.trim().substring(0, 80), class: el.className }))"
        )
        print(f"\n  🏷️ ラベル/見出し要素 ({len(labels)}件):")
        for lb in labels:
            if lb['text']:
                print(f"    <{lb['tag']} class='{lb['class'][:40]}'> {lb['text'][:60]}")

    # ページネーション探索
    pagination = await page.eval_on_selector_all(
        "a[href*='page'], .pagination a, nav a, [class*='pager'] a",
        "els => els.map(el => ({ href: el.href, text: el.textContent.trim() }))"
    )
    if pagination:
        print(f"\n  📑 ページネーション ({len(pagination)}件):")
        for p in pagination[:10]:
            print(f"    {p['text']} → {p['href']}")

    print(f"\n✅ 探索完了！ output/ フォルダを確認してセレクタを特定してください。")
    print(f"   - screenshots/  : 各画面のスクリーンショット")
    print(f"   - campaigns_list.html : 一覧ページのHTML")
    print(f"   - campaign_detail_sample.html : 詳細ページのHTML（サンプル）")


# ============================================================
# 抽出モード（scrape）
# ============================================================

# ★★★ ここのセレクタをdiscoverの結果に基づいて調整 ★★★
SELECTORS = {
    # キャンペーン一覧ページ
    "campaign_rows": "table tbody tr, .campaign-item, [class*='campaign'] .row",
    "campaign_link": "a[href*='/campaigns/']",
    "campaign_name": "td:first-child, .name, [class*='name']",
    "next_page": "a[rel='next'], .pagination .next a, a:has-text('次'), a:has-text('›')",

    # キャンペーン詳細ページ
    # discoverモードで実際のDOMを確認後に調整してください
    "lp_url_patterns": [
        "input[name*='lp'], input[name*='url'], input[name*='landing']",
        "a[href*='lp'], a[href*='landing']",
        "td:has-text('LP') + td, th:has-text('LP') ~ td",
        "[class*='lp'] input, [class*='url'] input",
    ],
    "upsell_url_patterns": [
        "input[name*='upsell'], input[name*='offer']",
        "a[href*='upsell'], a[href*='offer']",
        "td:has-text('アップセル') + td, th:has-text('アップセル') ~ td",
        "td:has-text('サンクス') + td, th:has-text('サンクス') ~ td",
    ],
}


async def get_all_campaign_links(page, delay: float) -> list[dict]:
    """一覧ページをページネーションしながら全キャンペーンリンクを収集"""
    all_campaigns = []
    page_num = 1

    await page.goto(CAMPAIGNS_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    while True:
        print(f"  📑 ページ {page_num} をスキャン中...")

        # キャンペーンリンクを収集
        links = await page.eval_on_selector_all(
            "a[href*='/campaigns/view/']",
            """els => els.map(el => ({
                href: el.href,
                text: el.textContent.trim().substring(0, 200)
            })).filter(l => l.href.match(/\\/campaigns\\/view\\/\\d+/))"""
        )

        # 重複排除しつつ追加
        existing_hrefs = {c['href'] for c in all_campaigns}
        new_links = [l for l in links if l['href'] not in existing_hrefs]
        all_campaigns.extend(new_links)
        print(f"    → {len(new_links)}件追加（累計: {len(all_campaigns)}件）")

        # 次のページへ
        next_btn = None
        for sel in ["a[rel='next']", ".pagination .next a", "a:has-text('次')", "a:has-text('›')", "a:has-text('Next')"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1000):
                    next_btn = el
                    break
            except Exception:
                continue

        if next_btn:
            await next_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(int(delay * 1000))
            page_num += 1
        else:
            print(f"  ✅ 全ページスキャン完了（{page_num}ページ）")
            break

    return all_campaigns


async def extract_campaign_detail(page, url: str, delay: float) -> dict:
    """キャンペーン詳細ページからLP/アップセルURL等を抽出"""
    result = {
        "url": url,
        "name": "",
        "lp_urls": [],
        "upsell_urls": [],
        "all_urls": [],
        "raw_text_snippets": [],
        "error": None,
    }

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(int(delay * 1000))

        # ページタイトル/見出しからキャンペーン名取得
        for sel in ["h1", "h2", ".campaign-name", "[class*='title']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1000):
                    result["name"] = (await el.text_content()).strip()
                    break
            except Exception:
                continue

        # 全input/textareaのvalue値を収集（URL含むフィールド）
        form_values = await page.eval_on_selector_all(
            "input, textarea, select",
            """els => els.map(el => ({
                name: el.name || el.id || '',
                type: el.type || el.tagName,
                value: (el.value || '').substring(0, 2000),
                label: el.labels?.[0]?.textContent?.trim() || ''
            })).filter(item => item.value)"""
        )

        # URL含むフィールドを分類
        for fv in form_values:
            val = fv['value']
            name_lower = (fv['name'] + fv['label']).lower()

            if re.match(r'https?://', val):
                result['all_urls'].append({
                    "field": fv['name'] or fv['label'],
                    "url": val
                })

                # LP判定
                if any(kw in name_lower for kw in ['lp', 'landing', 'url', 'ページ', 'page']):
                    result['lp_urls'].append(val)
                # アップセル判定
                elif any(kw in name_lower for kw in ['upsell', 'offer', 'アップセル', 'サンクス', 'thanks', 'cross']):
                    result['upsell_urls'].append(val)

        # テーブル/DL内のURL情報も抽出
        table_urls = await page.eval_on_selector_all(
            "td, dd, .value, [class*='value']",
            """els => els.map(el => {
                const text = el.textContent.trim();
                const prev = el.previousElementSibling;
                const label = prev ? prev.textContent.trim() : '';
                return { label, text: text.substring(0, 2000) };
            }).filter(item => item.text.match(/https?:\\/\\//))"""
        )

        for tu in table_urls:
            urls_found = re.findall(r'https?://[^\s<>"\']+', tu['text'])
            label_lower = tu['label'].lower()
            for u in urls_found:
                if u not in [x['url'] for x in result['all_urls']]:
                    result['all_urls'].append({"field": tu['label'], "url": u})

                if any(kw in label_lower for kw in ['lp', 'landing', 'url', 'ページ']):
                    if u not in result['lp_urls']:
                        result['lp_urls'].append(u)
                elif any(kw in label_lower for kw in ['upsell', 'offer', 'アップセル', 'サンクス']):
                    if u not in result['upsell_urls']:
                        result['upsell_urls'].append(u)

        # ページ内テキストからURL風文字列も補完抽出
        page_text = await page.eval_on_selector_all(
            "body *",
            """els => {
                const seen = new Set();
                return els.flatMap(el => {
                    if (el.children.length > 0) return [];
                    const text = el.textContent.trim();
                    const matches = text.match(/https?:\\/\\/[^\\s<>"']+/g) || [];
                    return matches.filter(m => {
                        if (seen.has(m)) return false;
                        seen.add(m);
                        return true;
                    });
                });
            }"""
        )
        for pu in page_text:
            if pu not in [x['url'] for x in result['all_urls']]:
                result['all_urls'].append({"field": "(テキスト内)", "url": pu})

    except PWTimeout:
        result['error'] = "タイムアウト"
    except Exception as e:
        result['error'] = str(e)

    return result


def generate_markdown(campaigns: list[dict], output_path: str):
    """抽出結果をMarkdownに出力"""
    lines = [
        f"# キャンペーン LP/アップセルURL一覧",
        f"",
        f"- 取得日時: {timestamp()}",
        f"- 取得元: {CAMPAIGNS_URL}",
        f"- 総キャンペーン数: {len(campaigns)}",
        f"",
        f"---",
        f"",
    ]

    # サマリーテーブル
    lines.append("## サマリー")
    lines.append("")
    lines.append("| # | キャンペーン名 | LP URL数 | アップセルURL数 | 全URL数 | エラー |")
    lines.append("|---|---|---|---|---|---|")
    for i, c in enumerate(campaigns, 1):
        error = c.get('error', '') or ''
        lines.append(
            f"| {i} | {c['name'][:40]} | {len(c['lp_urls'])} | {len(c['upsell_urls'])} | {len(c['all_urls'])} | {error} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各キャンペーン詳細
    for i, c in enumerate(campaigns, 1):
        lines.append(f"## {i}. {c['name'] or '(名称不明)'}")
        lines.append(f"")
        lines.append(f"- 管理画面URL: {c['url']}")
        lines.append(f"")

        if c.get('error'):
            lines.append(f"⚠️ **エラー**: {c['error']}")
            lines.append("")
            continue

        if c['lp_urls']:
            lines.append(f"### LP URL")
            for url in c['lp_urls']:
                lines.append(f"- {url}")
            lines.append("")

        if c['upsell_urls']:
            lines.append(f"### アップセルURL")
            for url in c['upsell_urls']:
                lines.append(f"- {url}")
            lines.append("")

        if c['all_urls']:
            lines.append(f"### 全URL一覧")
            lines.append(f"| フィールド | URL |")
            lines.append(f"|---|---|")
            for u in c['all_urls']:
                lines.append(f"| {u['field']} | {u['url']} |")
            lines.append("")

        lines.append("---")
        lines.append("")

    md_text = "\n".join(lines)
    Path(output_path).write_text(md_text, encoding="utf-8")
    print(f"\n💾 Markdown保存完了: {output_path} ({len(md_text):,}文字)")

    # JSON版も保存（プログラム的に使いやすい）
    json_path = output_path.replace('.md', '.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(campaigns, f, ensure_ascii=False, indent=2)
    print(f"💾 JSON保存完了: {json_path}")


async def scrape(page, account_id: str, login_id: str, password: str, output: str, delay: float, max_count: int | None):
    """全キャンペーンを巡回して情報抽出"""
    await login(page, account_id, login_id, password)

    # Step 1: キャンペーンリンク一覧を収集
    print(f"\n📋 キャンペーン一覧を収集中...")
    all_links = await get_all_campaign_links(page, delay)
    print(f"  → 合計 {len(all_links)}件のキャンペーンを発見")

    if max_count:
        all_links = all_links[:max_count]
        print(f"  → テスト: 最初の{max_count}件のみ処理")

    # Step 2: 各キャンペーンの詳細を取得
    results = []
    total = len(all_links)
    for i, link in enumerate(all_links, 1):
        print(f"\n[{i}/{total}] {link['text'][:50]}...")
        detail = await extract_campaign_detail(page, link['href'], delay)
        if not detail['name']:
            detail['name'] = link['text']
        results.append(detail)

        # LP/URL件数をリアルタイム表示
        lp_count = len(detail['lp_urls'])
        upsell_count = len(detail['upsell_urls'])
        all_count = len(detail['all_urls'])
        status = f"  → LP:{lp_count} / アップセル:{upsell_count} / 全URL:{all_count}"
        if detail.get('error'):
            status += f" ⚠️ {detail['error']}"
        print(status)

        # 中間保存（10件ごと）
        if i % 10 == 0:
            generate_markdown(results, output)
            print(f"  💾 中間保存 ({i}/{total}件)")

    # Step 3: 最終出力
    generate_markdown(results, output)

    # 統計
    total_lp = sum(len(c['lp_urls']) for c in results)
    total_upsell = sum(len(c['upsell_urls']) for c in results)
    total_urls = sum(len(c['all_urls']) for c in results)
    errors = sum(1 for c in results if c.get('error'))
    print(f"\n{'='*50}")
    print(f"📊 完了レポート")
    print(f"  キャンペーン数: {len(results)}")
    print(f"  LP URL: {total_lp}件")
    print(f"  アップセルURL: {total_upsell}件")
    print(f"  全URL: {total_urls}件")
    print(f"  エラー: {errors}件")
    print(f"  出力: {output}")
    print(f"{'='*50}")


# ============================================================
# メイン
# ============================================================
async def main():
    parser = argparse.ArgumentParser(description="Campaign Scraper for urerud2c.jp")
    parser.add_argument("mode", choices=["discover", "scrape"], help="実行モード")
    parser.add_argument("--account-id", required=True, help="アカウントID")
    parser.add_argument("--login-id", required=True, help="ログインID")
    parser.add_argument("--password", required=True, help="パスワード")
    parser.add_argument("--headed", action="store_true", help="ブラウザを表示して実行")
    parser.add_argument("--output", default="output/campaigns.md", help="出力ファイル名")
    parser.add_argument("--delay", type=float, default=1.5, help="ページ間の待機秒数")
    parser.add_argument("--max", type=int, default=None, help="最大取得件数（テスト用）")

    args = parser.parse_args()
    ensure_dirs()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.headed,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ja-JP",
        )
        page = await context.new_page()

        try:
            if args.mode == "discover":
                await discover(page, args.account_id, args.login_id, args.password)
            elif args.mode == "scrape":
                await scrape(page, args.account_id, args.login_id, args.password, args.output, args.delay, args.max)
        except Exception as e:
            # エラー時もスクショ保存
            await page.screenshot(path=str(SCREENSHOTS_DIR / "error.png"), full_page=True)
            print(f"\n❌ エラー発生: {e}")
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
