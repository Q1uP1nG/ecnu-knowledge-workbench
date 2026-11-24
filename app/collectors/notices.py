import re
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from ..attachments import Original, clean_filename
from .base import BaseCollector, FetchError, clean, link, preferred, text_of


def notice_date(text):
    match = re.search(r"(?<!\d)(20\d{2})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})(?!\d)", text)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return ""


def article_lines(article):
    lines = [text_of(node) for node in article.select("p,li,h2,h3,h4") if not node.find_parent(["table", "li"])]
    return list(dict.fromkeys(filter(None, lines))) or [text_of(article)]


def archived_originals(url, title, article, raw, selector):
    originals = [Original("page_html", url, clean_filename(f"{title}.html"), raw)]
    for anchor in article.select(selector):
        target = link(url, anchor.get("href"))
        if not target:
            continue
        path = PurePosixPath(unquote(urlsplit(target).path))
        if path.suffix.lower() not in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"}:
            continue
        label = text_of(anchor).strip("[]【】 ")
        if not label or label in {"下载", "查看", "点击下载"}:
            label = path.name
        if not label.lower().endswith(path.suffix.lower()):
            label += path.suffix
        originals.append(Original("policy_attachment", target, clean_filename(label)))
    return tuple({original.url: original for original in originals}.values())


class NoticeCollector(BaseCollector):
    cap_option = ""

    def matches(self, title, listing):
        raise NotImplementedError

    def entries(self, url, raw, listing_date):
        raise NotImplementedError

    def articles(self):
        selectors = self.config["selectors"]
        seen_articles = set()
        for listing in self.config["listings"]:
            url = listing["url"]
            host = urlsplit(url).hostname
            visited = set()
            while url and url not in visited and len(visited) < self.config["page_limit"]:
                page = self.html(url)
                visited.add(url)
                anchors = page.select(selectors["list_link"])
                if not anchors:
                    raise ValueError(f"公示列表没有命中配置的链接选择器：{url}")
                for anchor in anchors:
                    heading = clean(anchor.get("title")) or text_of(anchor)
                    target = link(url, anchor.get("href"), host)
                    if target and target not in seen_articles and self.matches(heading, listing):
                        seen_articles.add(target)
                        yield target, notice_date(text_of(anchor.find_parent("li")))
                following = next((a for a in page.select(selectors["next_page"]) if "下一页" in text_of(a)), None)
                url = link(url, following.get("href"), host) if following else ""

    def parts(self, url, raw, listing_date):
        soup = BeautifulSoup(raw, "lxml")
        selectors = self.config["selectors"]
        title = text_of(preferred(soup, selectors["title"]))
        article = preferred(soup, selectors["article"])
        if not title or article is None or not text_of(article):
            raise ValueError("未命中有标题和正文的详情页")
        for node in article.select("script, style, iframe, noscript"):
            node.decompose()
        published = notice_date(text_of(preferred(soup, selectors["published"]))) or listing_date
        return title, article, published, archived_originals(url, title, article, raw, selectors["attachment"])

    def collect(self):
        cap = self.options.get(self.cap_option)
        for index, (url, published) in enumerate(self.articles(), 1):
            try:
                for entry in self.entries(url, self.fetch(url, follow_redirects=False), published):
                    self.ctx.emit(entry)
            except FetchError as exc:
                if exc.status and 300 <= exc.status < 400:
                    self.ctx.log("info", "公示入口返回跳转，公开详情页跳过", url)
                    self.ctx.count("skipped")
                else:
                    self.ctx.failure(f"公示页请求失败：{exc}", url)
            except (ValueError, RuntimeError) as exc:
                self.ctx.failure(f"公示页采集失败：{exc}", url)
            if cap and index >= cap:
                break
