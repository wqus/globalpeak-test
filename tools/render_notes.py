"""Tiny renderer for our own controlled Markdown, not a general Markdown engine."""
import html
import re


def inline(text):
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f'<a href="{m[2].replace(".md", ".html")}">{m[1]}</a>', text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def render(markdown):
    result, paragraph, table, listing = [], [], [], None
    code = None

    def flush():
        nonlocal listing
        if paragraph:
            result.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
        if table:
            rows = [r for r in table if not re.fullmatch(r"[\s|:\-]+", r)]
            output = ['<div class="note-table"><table>']
            for i, row in enumerate(rows):
                tag = 'th' if i == 0 else 'td'
                output.append('<tr>' + ''.join(f'<{tag}>{inline(c.strip())}</{tag}>' for c in row.strip('|').split('|')) + '</tr>')
            output.append('</table></div>')
            result.extend(output)
            table.clear()
        if listing:
            result.append(f'</{listing}>')
            listing = None

    for line in markdown.splitlines():
        if line.startswith('```'):
            flush()
            if code is None:
                code = []
            else:
                result.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
                code = None
        elif code is not None:
            code.append(line)
        elif not line.strip():
            flush()
        elif line.startswith('|'):
            if paragraph or listing:
                flush()
            table.append(line)
        elif re.match(r'^#{1,4} ', line):
            flush()
            level = len(line) - len(line.lstrip('#'))
            result.append(f'<h{level}>' + inline(line[level:].strip()) + f'</h{level}>')
        elif re.match(r'^(- |\d+\. )', line):
            if paragraph or table:
                flush()
            kind = 'ul' if line.startswith('- ') else 'ol'
            if listing != kind:
                flush()
                result.append(f'<{kind}>')
                listing = kind
            result.append('<li>' + inline(re.sub(r'^(- |\d+\. )', '', line)) + '</li>')
        elif line == '---':
            flush()
            result.append('<hr>')
        else:
            if table or listing:
                flush()
            paragraph.append(line)
    flush()
    return '\n'.join(result)


def page(title, body, filename):
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} / GlobalPeak</title><link rel="icon" href="../assets/mark.svg"><link rel="stylesheet" href="../styles.css"><link rel="stylesheet" href="../notes.css"></head><body><header class="note-header"><a href="../index.html">← Отчёт / Абрамян Бен</a><a href="../data/{filename}" download>Исходник Markdown ↓</a></header><main class="note-body">{body}</main><footer class="note-footer"><a href="../index.html">← К демонстрациям</a><span>GlobalPeak / тестовое / 03.11.2026</span></footer></body></html>'''
