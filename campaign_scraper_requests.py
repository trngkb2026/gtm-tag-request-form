#!/usr/bin/env python3
"""
Campaign Scraper for urerud2c.jp (requests版)
==============================================
Playwrightを使わず、requests + BeautifulSoup で
キャンペーン一覧→各詳細ページを巡回して
LP URL・サンクスURLを抽出し campaigns.json に保存する。

Usage:
  # 抽出モード
  python campaign_scraper_requests.py scrape \
    --account-id ACCT --login-id USER --password PASS

  # 探索モード（DOM構造を確認）
  python campaign_scraper_requests.py discover \
    --account-id ACCT --login-id USER --password PASS

  # オプション
  --output FILE   出力ファイル名（デフォルト: output/campaigns.json）
  --delay SEC     ページ間の待機秒数（デフォルト: 1.0）
  --max N         最大取得件数（テスト用）
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ requests未インストール: pip install requests")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("❌ beautifulsoup4未インストール: pip install beautifulsoup4")
    sys.exit(1)

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://urerud2c.jp"
LOGIN_URL = f"{BASE_URL}/login"
CAMPAIGNS_URL = f"{BASE_URL}/admin/campaigns"
OUTPUT_DIR = Path("output")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}


# ============================================================
# ユーティリティ
# ============================================================
def ensure_dirs():
    OUTPUT_DIR.mkdir(exist_ok=True)


def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# ログイン
# ============================================================
def login(session: requests.Session, account_id: str, login_id: str, password: str):
    """管理画面にログインしてセッションCookieを取得する"""
    print(f"🔐 ログイン中... {LOGIN_URL}")

    # Step 1: ログインページを取得（CSRFトークン取得）
    resp = session.get(LOGIN_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # CSRFトークンを探す
    csrf_token = None
    csrf_input = soup.find("input", {"name": re.compile(r"csrf|_token|authenticity_token", re.I)})
    if csrf_input:
        csrf_token = csrf_input.get("value", "")
        print(f"  ✓ CSRFトークン発見: {csrf_input['name']}")

    # meta タグからCSRFトークンを探す（Rails系）
    if not csrf_token:
        meta_csrf = soup.find("meta", {"name": re.compile(r"csrf-token", re.I)})
        if meta_csrf:
            csrf_token = meta_csrf.get("content", "")
            print(f"  ✓ CSRFトークン(meta): {csrf_token[:20]}...")

    # フォームのaction URLを特定
    form = soup.find("form", {"action": re.compile(r"login|session", re.I)})
    if not form:
        form = soup.find("form")
    action_url = form.get("action", LOGIN_URL) if form else LOGIN_URL
    if action_url.startswith("/"):
        action_url = BASE_URL + action_url

    # フォーム内の全input要素を収集（hidden含む）
    form_data = {}
    if form:
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name:
                form_data[name] = value

    # ログインフィールドを推定して設定
    # account_id / login_id / password のフィールド名を探す
    input_fields = form.find_all("input") if form else []
    text_fields = [
        f for f in input_fields
        if f.get("type", "text") in ("text", "email", "")
        and f.get("name", "") not in ("", "utf8")
        and not f.get("name", "").startswith("_")
    ]
    pass_fields = [f for f in input_fields if f.get("type") == "password"]

    if len(text_fields) >= 2 and len(pass_fields) >= 1:
        # 3フィールドログイン: account_id, login_id, password
        form_data[text_fields[0].get("name")] = account_id
        form_data[text_fields[1].get("name")] = login_id
        form_data[pass_fields[0].get("name")] = password
        print(f"  ✓ 3フィールドログイン: {text_fields[0].get('name')}, {text_fields[1].get('name')}, {pass_fields[0].get('name')}")
    elif len(text_fields) >= 1 and len(pass_fields) >= 1:
        # 2フィールドログイン
        form_data[text_fields[0].get("name")] = login_id
        form_data[pass_fields[0].get("name")] = password
        print(f"  ✓ 2フィールドログイン: {text_fields[0].get('name')}, {pass_fields[0].get('name')}")
    else:
        # フォールバック: 一般的なフィールド名を試す
        form_data.update({
            "account_id": account_id,
            "login_id": login_id,
            "password": password,
        })
        print("  ⚠ フォールバック: 汎用フィールド名で送信")

    # Step 2: ログインPOST
    login_headers = {**HEADERS, "Referer": LOGIN_URL}
    resp = session.post(
        action_url,
        data=form_data,
        headers=login_headers,
        timeout=30,
        allow_redirects=True,
    )

    # ログイン成功確認
    if "login" in resp.url.lower() and resp.url != action_url:
        # ログインページにリダイレクトされた = 失敗
        (OUTPUT_DIR / "login_failed.html").write_text(resp.text, encoding="utf-8")
        raise RuntimeError(f"ログイン失敗。login_failed.html を確認してください。URL: {resp.url}")

    print(f"  ✅ ログイン成功！ → {resp.url}")
    return session


# ============================================================
# 探索モード
# ============================================================
def discover(session: requests.Session):
    """DOM構造を探索してHTMLを保存"""
    print(f"\n📋 キャンペーン一覧を取得中... {CAMPAIGNS_URL}")
    resp = session.get(CAMPAIGNS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    (OUTPUT_DIR / "campaigns_list.html").write_text(resp.text, encoding="utf-8")
    print(f"  💾 campaigns_list.html 保存完了")

    soup = BeautifulSoup(resp.text, "html.parser")

    # キャンペーンリンクを探す
    links = soup.find_all("a", href=re.compile(r"/campaigns/view/\d+"))
    print(f"\n  🔗 キャンペーンリンク: {len(links)}件")
    for i, link in enumerate(links[:20]):
        href = link.get("href", "")
        text = link.get_text(strip=True)[:60]
        print(f"    [{i}] {text} → {href}")

    # ページネーション
    pager_links = soup.find_all("a", href=re.compile(r"page=\d+"))
    if pager_links:
        pages = set()
        for pl in pager_links:
            m = re.search(r"page=(\d+)", pl.get("href", ""))
            if m:
                pages.add(int(m.group(1)))
        print(f"\n  📑 ページネーション: {sorted(pages)}")

    # 最初のキャンペーン詳細を確認
    if links:
        first_href = links[0].get("href", "")
        detail_url = first_href if first_href.startswith("http") else BASE_URL + first_href
        print(f"\n📄 最初のキャンペーン詳細を確認... {detail_url}")
        resp2 = session.get(detail_url, headers=HEADERS, timeout=30)
        (OUTPUT_DIR / "campaign_detail_sample.html").write_text(resp2.text, encoding="utf-8")
        print(f"  💾 campaign_detail_sample.html 保存完了")

        soup2 = BeautifulSoup(resp2.text, "html.parser")

        # URL風の文字列を全抽出
        urls_found = set()
        for el in soup2.find_all(["input", "a", "td", "dd"]):
            for val in [el.get("href", ""), el.get("value", ""), el.get_text()]:
                for u in re.findall(r"https?://[^\s<>\"']+", val):
                    urls_found.add(u)
        print(f"\n  🌐 ページ内URL ({len(urls_found)}件):")
        for u in sorted(urls_found):
            print(f"    {u}")

    print(f"\n✅ 探索完了！ output/ フォルダを確認してセレクタを特定してください。")


# ============================================================
# 抽出モード
# ============================================================
def get_all_campaign_links(session: requests.Session, delay: float) -> list[dict]:
    """一覧ページをページネーションしながら全キャンペーンリンクを収集"""
    all_campaigns = []
    page_num = 1
    seen_hrefs = set()

    url = CAMPAIGNS_URL

    while True:
        print(f"  📑 ページ {page_num} をスキャン中... {url}")
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # キャンペーンリンクを収集
        links = soup.find_all("a", href=re.compile(r"/campaigns/view/\d+"))
        new_count = 0
        for link in links:
            href = link.get("href", "")
            if not href.startswith("http"):
                href = BASE_URL + href
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            text = link.get_text(strip=True)[:200]
            all_campaigns.append({"href": href, "text": text})
            new_count += 1

        print(f"    → {new_count}件追加（累計: {len(all_campaigns)}件）")

        # 次のページを探す
        next_link = None
        for sel in [
            soup.find("a", rel="next"),
            soup.find("a", string=re.compile(r"次|›|Next")),
        ]:
            if sel:
                next_link = sel
                break

        # .pagination 内の次のリンクも探す
        if not next_link:
            pagination = soup.find(class_=re.compile(r"pagination|pager"))
            if pagination:
                current = pagination.find(class_=re.compile(r"active|current"))
                if current:
                    next_sibling = current.find_next_sibling()
                    if next_sibling:
                        a = next_sibling.find("a") if next_sibling.name != "a" else next_sibling
                        if a and a.get("href"):
                            next_link = a

        if next_link and next_link.get("href"):
            next_url = next_link["href"]
            if not next_url.startswith("http"):
                next_url = BASE_URL + next_url
            url = next_url
            page_num += 1
            time.sleep(delay)
        else:
            print(f"  ✅ 全ページスキャン完了（{page_num}ページ）")
            break

    return all_campaigns


def extract_campaign_detail(session: requests.Session, url: str, delay: float) -> dict:
    """キャンペーン詳細ページからLP/サンクスURL等を抽出"""
    result = {
        "url": url,
        "name": "",
        "campaign_id": "",
        "lp_urls": [],
        "upsell_urls": [],
        "all_urls": [],
        "error": None,
    }

    # URLからcampaign_id抽出
    cid_match = re.search(r"/campaigns/view/(\d+)", url)
    if cid_match:
        result["campaign_id"] = cid_match.group(1)

    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        time.sleep(delay)

        soup = BeautifulSoup(resp.text, "html.parser")

        # ページタイトル/見出しからキャンペーン名取得
        for sel in ["h1", "h2", ".campaign-name"]:
            el = soup.select_one(sel)
            if el:
                result["name"] = el.get_text(strip=True)
                break

        # 全input/textareaのvalue値を収集
        for el in soup.find_all(["input", "textarea"]):
            val = el.get("value", "") or el.get_text(strip=True)
            name = el.get("name", "") or el.get("id", "")
            label = ""
            # ラベルを探す
            label_el = el.find_previous("label")
            if label_el:
                label = label_el.get_text(strip=True)

            if not val or not re.match(r"https?://", val):
                continue

            field_name = name or label or "(unknown)"
            result["all_urls"].append({"field": field_name, "url": val})
            name_lower = (name + label).lower()

            if any(kw in name_lower for kw in ["lp", "landing", "url", "ページ", "page"]):
                if val not in result["lp_urls"]:
                    result["lp_urls"].append(val)
            elif any(kw in name_lower for kw in ["upsell", "offer", "アップセル", "サンクス", "thanks"]):
                if val not in result["upsell_urls"]:
                    result["upsell_urls"].append(val)

        # テーブル/DL内のURLも抽出
        for el in soup.find_all(["td", "dd"]):
            text = el.get_text(strip=True)
            urls_found = re.findall(r"https?://[^\s<>\"']+", text)
            if not urls_found:
                continue

            prev = el.find_previous_sibling()
            label = prev.get_text(strip=True) if prev else ""
            label_lower = label.lower()

            for u in urls_found:
                if u not in [x["url"] for x in result["all_urls"]]:
                    result["all_urls"].append({"field": label or "(table)", "url": u})

                if any(kw in label_lower for kw in ["lp", "landing", "url", "ページ"]):
                    if u not in result["lp_urls"]:
                        result["lp_urls"].append(u)
                elif any(kw in label_lower for kw in ["upsell", "offer", "アップセル", "サンクス"]):
                    if u not in result["upsell_urls"]:
                        result["upsell_urls"].append(u)

        # ページ全体からURL補完抽出
        page_text = soup.get_text()
        for u in re.findall(r"https?://[^\s<>\"']+", page_text):
            if u not in [x["url"] for x in result["all_urls"]]:
                result["all_urls"].append({"field": "(text)", "url": u})

    except requests.Timeout:
        result["error"] = "タイムアウト"
    except Exception as e:
        result["error"] = str(e)

    return result


def scrape(
    session: requests.Session,
    output: str,
    delay: float,
    max_count: int | None,
):
    """全キャンペーンを巡回して情報抽出"""

    # Step 1: キャンペーンリンク一覧を収集
    print(f"\n📋 キャンペーン一覧を収集中...")
    all_links = get_all_campaign_links(session, delay)
    print(f"  → 合計 {len(all_links)}件のキャンペーンを発見")

    if max_count:
        all_links = all_links[:max_count]
        print(f"  → テスト: 最初の{max_count}件のみ処理")

    # Step 2: 各キャンペーンの詳細を取得
    results = []
    total = len(all_links)
    for i, link in enumerate(all_links, 1):
        print(f"\n[{i}/{total}] {link['text'][:50]}...")
        detail = extract_campaign_detail(session, link["href"], delay)
        if not detail["name"]:
            detail["name"] = link["text"]
        results.append(detail)

        lp_count = len(detail["lp_urls"])
        upsell_count = len(detail["upsell_urls"])
        all_count = len(detail["all_urls"])
        status = f"  → LP:{lp_count} / アップセル:{upsell_count} / 全URL:{all_count}"
        if detail.get("error"):
            status += f" ⚠️ {detail['error']}"
        print(status)

        # 中間保存（10件ごと）
        if i % 10 == 0:
            save_results(results, output)
            print(f"  💾 中間保存 ({i}/{total}件)")

    # Step 3: 最終出力
    save_results(results, output)

    # 統計
    total_lp = sum(len(c["lp_urls"]) for c in results)
    total_upsell = sum(len(c["upsell_urls"]) for c in results)
    total_urls = sum(len(c["all_urls"]) for c in results)
    errors = sum(1 for c in results if c.get("error"))
    print(f"\n{'='*50}")
    print(f"📊 完了レポート")
    print(f"  キャンペーン数: {len(results)}")
    print(f"  LP URL: {total_lp}件")
    print(f"  アップセルURL: {total_upsell}件")
    print(f"  全URL: {total_urls}件")
    print(f"  エラー: {errors}件")
    print(f"  出力: {output}")
    print(f"{'='*50}")


def save_results(campaigns: list[dict], output_path: str):
    """JSON保存"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(campaigns, f, ensure_ascii=False, indent=2)
    print(f"💾 JSON保存完了: {output_path} ({len(campaigns)}件)")


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Campaign Scraper for urerud2c.jp (requests版 / Playwright不要)"
    )
    parser.add_argument("mode", choices=["discover", "scrape"], help="実行モード")
    parser.add_argument("--account-id", required=True, help="アカウントID")
    parser.add_argument("--login-id", required=True, help="ログインID")
    parser.add_argument("--password", required=True, help="パスワード")
    parser.add_argument(
        "--output",
        default="output/campaigns.json",
        help="出力ファイル名（デフォルト: output/campaigns.json）",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0, help="ページ間の待機秒数（デフォルト: 1.0）"
    )
    parser.add_argument(
        "--max", type=int, default=None, help="最大取得件数（テスト用）"
    )

    args = parser.parse_args()
    ensure_dirs()

    session = requests.Session()
    login(session, args.account_id, args.login_id, args.password)

    if args.mode == "discover":
        discover(session)
    elif args.mode == "scrape":
        scrape(session, args.output, args.delay, args.max)


if __name__ == "__main__":
    main()
