#!/usr/bin/env python3
"""
LP名リスト＆campaign_idマッピングを campaign_scraper.py の出力JSONから更新し、
gtm-tag-request-form の index.html を書き換えてGitHub Pushする。

Usage:
  python update_form_lps.py --input output/campaigns.json

前提:
  - campaign_scraper.py scrape 実行済み → campaigns.json が存在
  - 環境変数 GITHUB_TOKEN が設定済み（またはSupabase Vault経由）
"""

import argparse
import base64
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests

# ============================================================
# 設定
# ============================================================
GITHUB_REPO = "trngkb2026/gtm-tag-request-form"
GITHUB_FILE = "index.html"
LP_DOMAIN = "cp.s-herb.com"


def get_github_token() -> str:
    """環境変数 or Supabase Vault からGitHubトークン取得"""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    # Vault fallback
    try:
        import subprocess
        result = subprocess.run(
            ["supabase", "secrets", "get", "github_pat"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    raise RuntimeError("GITHUB_TOKEN が見つかりません。環境変数に設定してください。")


def parse_campaigns(json_path: str) -> tuple[list[str], dict[str, str]]:
    """
    campaigns.json を解析してLP名リストとcampaign_idマッピングを返す。

    Returns:
        lp_names: ソート済みLP名リスト
        campaign_map: {lp_name: campaign_id}
    """
    with open(json_path, "r", encoding="utf-8") as f:
        campaigns = json.load(f)

    lp_names = set()
    campaign_map = {}

    for c in campaigns:
        # 管理画面URL からcampaign_idを抽出
        admin_url = c.get("url", "")
        cid_match = re.search(r"/campaigns/view/(\d+)", admin_url)
        campaign_id = cid_match.group(1) if cid_match else ""

        # LP URLからLP名を抽出
        for url in c.get("lp_urls", []):
            match = re.match(rf"https?://{re.escape(LP_DOMAIN)}/(.+?)(?:\?|$)", url)
            if match:
                lp_name = match.group(1).rstrip("/")
                if lp_name and not lp_name.startswith("orders/"):
                    lp_names.add(lp_name)
                    if campaign_id:
                        campaign_map[lp_name] = campaign_id

        # all_urls からも補完
        for u in c.get("all_urls", []):
            url = u.get("url", "")
            match = re.match(rf"https?://{re.escape(LP_DOMAIN)}/(.+?)(?:\?|$)", url)
            if match:
                lp_name = match.group(1).rstrip("/")
                if (
                    lp_name
                    and not lp_name.startswith("orders/")
                    and not lp_name.startswith("admin/")
                    and not lp_name.startswith("login")
                ):
                    lp_names.add(lp_name)
                    if campaign_id and lp_name not in campaign_map:
                        campaign_map[lp_name] = campaign_id

    sorted_names = sorted(lp_names)
    print(f"📊 LP名: {len(sorted_names)}件, campaign_id: {len(campaign_map)}件")
    return sorted_names, campaign_map


def build_lp_groups(lp_names: list[str]) -> dict[str, list[str]]:
    """LP名をプレフィックスでグルーピング"""
    GROUP_PATTERNS = [
        ("PKG1J", r"^PKG1J($|_)"),
        ("PKG1N", r"^PKG1N"),
        ("PKG1G", r"^PKG1G"),
        ("PKG1L", r"^PKG1L"),
        ("PKG1Q", r"^PKG1Q"),
        ("PKG1R", r"^PKG1R"),
        ("PKG1KS", r"^PKG1KS"),
        ("PKG1_IC", r"^PKG1_IC"),
        ("PKG1他", r"^PKG1[IMOPSTU]"),
        ("PKG3C", r"^PKG3C"),
        ("PKG3E", r"^PKG3E"),
        ("PKG3D", r"^PKG3D"),
        ("PKG3K", r"^PKG3[IJ K]"),
        ("PC1A", r"^PC1[AB]"),
        ("PC15D", r"^PC15D"),
        ("PCPKG", r"^PCPKG"),
        ("PK22H", r"^PK22H"),
        ("PK22J", r"^PK22J"),
        ("PK22L", r"^PK22L"),
        ("MPC1A", r"^MPC1A"),
        ("MPK", r"^MPK"),
        ("PLTP", r"^PLTP"),
        ("その他", r".*"),
    ]

    groups = {}
    assigned = set()
    for gname, pattern in GROUP_PATTERNS:
        members = [lp for lp in lp_names if re.match(pattern, lp) and lp not in assigned]
        if members:
            groups[gname] = members
            assigned.update(members)

    return groups


def update_html(html: str, lp_names: list[str], campaign_map: dict, groups: dict) -> str:
    """index.html 内のLP_NAMES, LP_CAMPAIGN_MAP, LP_GROUPS, グループボタンを更新"""

    # 1. LP_NAMES 更新
    new_lp_names = "const LP_NAMES = " + json.dumps(lp_names, ensure_ascii=False) + ";"
    html = re.sub(r"const LP_NAMES = \[.*?\];", new_lp_names, html, flags=re.DOTALL)

    # 2. LP_CAMPAIGN_MAP 更新
    new_map = "const LP_CAMPAIGN_MAP = " + json.dumps(campaign_map, ensure_ascii=False) + ";"
    html = re.sub(r"const LP_CAMPAIGN_MAP = \{.*?\};", new_map, html)

    # 3. LP_GROUPS 更新
    new_groups = "const LP_GROUPS = " + json.dumps(groups, ensure_ascii=False) + ";"
    html = re.sub(r"const LP_GROUPS = \{.*?\};", new_groups, html)

    # 4. グループボタンHTML更新
    total = len(lp_names)
    buttons = [f'          <button type="button" class="lp-group-btn active" data-group="all">全て<span class="cnt">{total}</span></button>']
    for gname, members in groups.items():
        buttons.append(
            f'          <button type="button" class="lp-group-btn" data-group="{gname}">{gname}<span class="cnt">{len(members)}</span></button>'
        )
    buttons_html = "\n".join(buttons)

    html = re.sub(
        r'(<div class="lp-group-bar" id="lpGroupBar">)\n.*?(</div>)',
        rf'\1\n{buttons_html}\n        \2',
        html,
        flags=re.DOTALL,
    )

    return html


def github_push(token: str, html: str, message: str):
    """GitHub APIでindex.htmlを更新"""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get current SHA
    res = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}",
        headers=headers,
    )
    res.raise_for_status()
    sha = res.json()["sha"]

    # Push
    content_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    res = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}",
        headers=headers,
        json={"message": message, "content": content_b64, "sha": sha},
    )
    res.raise_for_status()
    print(f"✅ GitHub Push完了: {res.json()['commit']['sha'][:8]}")


def main():
    parser = argparse.ArgumentParser(description="LP名リストをフォームに反映")
    parser.add_argument("--input", required=True, help="campaigns.json のパス")
    parser.add_argument("--dry-run", action="store_true", help="GitHub Pushせずローカル出力のみ")
    args = parser.parse_args()

    # 1. campaigns.json 解析
    lp_names, campaign_map = parse_campaigns(args.input)
    groups = build_lp_groups(lp_names)

    print(f"\n📋 グループ:")
    for g, members in groups.items():
        print(f"   {g}: {len(members)}件")

    # 2. 現在のindex.htmlを取得
    token = get_github_token()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    res = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}",
        headers=headers,
    )
    res.raise_for_status()
    current_html = base64.b64decode(res.json()["content"]).decode("utf-8")

    # 3. HTML更新
    updated_html = update_html(current_html, lp_names, campaign_map, groups)

    if args.dry_run:
        Path("output/index_updated.html").write_text(updated_html, encoding="utf-8")
        print(f"💾 dry-run: output/index_updated.html に保存")
        return

    # 4. GitHub Push
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    github_push(
        token,
        updated_html,
        f"chore: LP名リスト週次更新 ({date_str}, {len(lp_names)}件)",
    )


if __name__ == "__main__":
    main()
