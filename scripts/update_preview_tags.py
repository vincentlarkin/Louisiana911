"""Point each page's link-preview tags at its card in app.PAGE_CARDS.

Run after changing a card, a parish outline or SHARE_CARD_VERSION, then commit
the HTML. tests/test_canonical_origin.py fails until the tags match. No
network access; the cards are drawn when first requested.
"""
import html
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PAGE_FILES = {
    'index.html': 'louisiana',
    'about.html': 'louisiana',
    'privacy.html': 'louisiana',
    'coverage.html': 'coverage',
    'reports.html': 'reports',
    'caddo911.html': 'caddo',
    'coverage-baton-rouge.html': 'baton-rouge',
    'coverage-lafayette.html': 'lafayette',
    'coverage-new-orleans.html': 'new-orleans',
    'coverage-lake-charles.html': 'lake-charles',
    'coverage-sulphur.html': 'sulphur',
}

_DROPPED = re.compile(r'<meta (?:property="og:image:(?:type|width|height|alt)"|name="twitter:image:alt") ')


def update_page(text: str, slug: str, app) -> str:
    """Return the page with its og:image and twitter:image tags pointing at the card."""
    url = html.escape(app._page_image_url(slug), quote=True)
    alt = html.escape(app.PAGE_CARDS[slug][1], quote=True)
    og_tags = [
        f'<meta property="og:image" content="{url}">',
        '<meta property="og:image:type" content="image/jpeg">',
        f'<meta property="og:image:width" content="{app.share_image.WIDTH}">',
        f'<meta property="og:image:height" content="{app.share_image.HEIGHT}">',
        f'<meta property="og:image:alt" content="{alt}">',
    ]
    twitter_tags = [
        f'<meta name="twitter:image" content="{url}">',
        f'<meta name="twitter:image:alt" content="{alt}">',
    ]
    lines = []
    for line in text.split('\n'):
        indent = line[:len(line) - len(line.lstrip())]
        stripped = line.strip()
        if _DROPPED.match(stripped):
            continue
        if stripped.startswith('<meta property="og:image" '):
            lines.extend(indent + tag for tag in og_tags)
        elif stripped.startswith('<meta name="twitter:image" '):
            lines.extend(indent + tag for tag in twitter_tags)
        else:
            lines.append(line)
    return '\n'.join(lines)


def main():
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory() as scratch:
        os.environ.setdefault('LOUISIANA911_DB_PATH', os.path.join(scratch, 'preview-tags.db'))
        import app
        changed = 0
        for name, slug in PAGE_FILES.items():
            path = ROOT / 'public' / name
            text = path.read_text(encoding='utf-8')
            updated = update_page(text, slug, app)
            if updated != text:
                path.write_text(updated, encoding='utf-8')
                changed += 1
        print(f'Updated preview tags in {changed} of {len(PAGE_FILES)} pages')


if __name__ == '__main__':
    main()
