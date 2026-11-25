import re
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from ..attachments import Original
from ..db import digest, news_items
from ..store import Entry
from .base import BaseCollector, clean, link, preferred, text_of

DATE = re.compile(r"\b(20\d{2})[-./年](\d{1,2})[-./月](\d{1,2})(?:日|\b)")
NUMBERED = re.compile(r"^(?:[一二三四五六七八九十]+[、．.]|第[一二三四五六七八九十\d]+[章节]|\d+[、.]\s*[\u3400-\u9fff])")
COMPANY_SUFFIX = r"(?:股份有限公司|有限责任公司|有限公司)"
COMPANY_NAMES = re.compile(rf"[\u3400-\u9fffA-Za-z0-9（）()·-]+?{COMPANY_SUFFIX}")
COMPANY_PREFIX = re.compile(
    r"园区高新技术企业|高新技术企业|专精特新(?:企业)?|在孵企业|入驻企业|园区企业|"
    r"联合|携手|邀请|特邀|授予|主办|协办|本次获奖的|获奖的|参加活动的|[由与和的](?=上海)")
COMPANY_CONTEXT = re.compile(r"在孵|入驻|园区|高新技术企业|专精特新|签署|合作|路演|获奖|协办|主讲|总经理|创始人")
COMPANY_EXCLUDED = re.compile(r"科技园管理|大学科技园|建设银行|工商银行|中国银行|交通银行|农业银行|招商银行|科技园")
COMPANY_INTRO = re.compile(r"是一家|主营|专注(?:于)?|致力于|主要从事|核心业务|提供[^。；]{1,60}解决方案|主要业务")
PARSER_VERSION = 4
FIELD_WORDS = ("人工智能", "医疗器械", "芯片", "机器人", "生物医药", "智能制造", "新能源", "新材料", "软件", "传感器")


def read_date(text):
    match = DATE.search(text or "")
    return f"{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}" if match else ""


class ScienceParkCollector(BaseCollector):
    @property
    def selectors(self):
        return self.config["selectors"]

    def collect(self):
        if self.options.get("replay_cache"):
            self.replay()
            return
        for category, urls in self.config["service_urls"].items():
            for url in urls:
                try:
                    self.service_url(url, category)
                except Exception as exc:
                    self.ctx.failure(f"服务页解析异常：{exc}", url)
        self.news()

    def service_url(self, url, category):
        raw = self.fetch(url)
        soup = BeautifulSoup(raw, "lxml")
        listing = soup.select_one(self.selectors["service_list"])
        if listing is not None and preferred(soup, self.selectors["article"]) is None:
            links = list(dict.fromkeys(link(url, a.get("href"), "kjy.ecnu.edu.cn")
                                       for a in listing.select('a[href*="/page.htm"]')))
            if not any(links) and not text_of(listing):
                self.ctx.log("info", f"{self.title(soup)}栏目当前无正文或文章", url)
                self.ctx.count("skipped")
                return
            for target in filter(None, links):
                try:
                    content = self.fetch(target)
                    self.service(target, category, BeautifulSoup(content, "lxml"), content)
                except Exception as exc:
                    self.ctx.failure(f"服务文章解析异常：{exc}", target)
            if any(links):
                return
        self.service(url, category, soup, raw)

    def replay(self):
        categories = {url: category for category, urls in self.config["service_urls"].items() for url in urls}
        news_path = urlsplit(self.config["news_url"]).path
        cache = {}
        for url, body in self.cached("text/html"):
            cache.setdefault(url, body)
        news = {}
        service_links = dict(categories)
        for url, content in cache.items():
            path = urlsplit(url).path
            if re.fullmatch(re.escape(news_path.removesuffix(".htm")) + r"\d*\.htm", path):
                listing = BeautifulSoup(content, "lxml")
                for anchor in listing.select(self.selectors["news_link"]):
                    target = link(url, anchor.get("href"), "kjy.ecnu.edu.cn")
                    if target:
                        news[target] = read_date(text_of(anchor.find_parent("li")))
            if url in categories:
                listing = BeautifulSoup(content, "lxml").select_one(self.selectors["service_list"])
                if listing:
                    service_links.update({target: categories[url] for a in listing.select(self.selectors["service_link"])
                                          if (target := link(url, a.get("href"), "kjy.ecnu.edu.cn"))})
        self.ctx.log("info", f"本地快照重解析 · {len(news)} 篇园区新闻")
        for url, category in service_links.items():
            if url in cache:
                soup = BeautifulSoup(cache[url], "lxml")
                if preferred(soup, self.selectors["article"]):
                    self.service(url, category, soup, cache[url])
        for url, evidence_date in news.items():
            if url in cache:
                self.news_article(url, evidence_date, cache[url], replay=True)

    def article(self, soup):
        element = preferred(soup, self.selectors["article"])
        if element is None:
            raise ValueError("没有匹配到正文容器")
        for unwanted in element.select("script, style, svg, iframe, noscript, .possplit"):
            unwanted.decompose()
        return element

    def title(self, soup):
        heading = text_of(preferred(soup, self.selectors["title"]))
        return heading or text_of(soup.title)

    def service(self, url, category, soup, raw=None):
        title = self.title(soup)
        article = self.article(soup)
        headings = {id(node) for node in article.select(self.selectors["headings"])}
        sections, body, chapter = [], [], []
        heading = title
        level = 1
        parents = {}
        media_count = 0
        originals = []
        if raw:
            originals.append(Original("page_html", url, f"{title}.html", raw))

        def finish():
            if chapter:
                trail = " / ".join(parents[i] for i in sorted(parents) if i < level)
                sections.append({"章节名称": f"{trail} / {heading}" if trail else heading,
                                 "章节标题": heading, "章节分类": category, "章节原文": "\n".join(chapter)})
                chapter.clear()

        def start_section(label, depth):
            nonlocal heading, level, parents
            finish()
            heading, level = label, depth
            parents = {i: name for i, name in parents.items() if i < level}
            parents[level] = heading

        for node in article.select("h1,h2,h3,h4,h5,h6,p,li,table,img,a[href]"):
            if node.find_parent(["table", "li"]) and node.name != "img":
                continue
            if node.name == "img":
                image = link(url, node.get("src"))
                if image:
                    line = f"[原页图片] {image}"
                    media_count += 1
                    originals.append(Original("page_image", image))
                else:
                    continue
            elif node.name == "a":
                target = link(url, node.get("href"))
                if not target or not re.search(r"\.(?:pdf|docx?|xlsx?|zip)(?:$|\?)", target, re.I):
                    continue
                line = f"[附件] {text_of(node)} {target}"
                originals.append(Original("policy_attachment", target))
            else:
                line = text_of(node)
                if not line:
                    continue
                if node.name == "table":
                    first_row = node.find("tr")
                    cells = first_row.find_all(["td", "th"], recursive=False) if first_row else []
                    label = text_of(node.find("caption", recursive=False))
                    if not label and len(cells) == 1 and re.fullmatch(r"[2-9]\d*", cells[0].get("colspan", "")):
                        label = text_of(cells[0])
                    if 2 <= len(label) <= 36 and label != heading:
                        start_section(label, 2)
                is_heading = 2 <= len(line) <= 36 and not line.startswith(("（", "(")) and not line.endswith("。") and (
                    id(node) in headings or node.name == "p" and (
                        NUMBERED.match(line) or node.find(["strong", "b"]) and text_of(node.find(["strong", "b"])) == line))
                if is_heading:
                    depth = int(node.name[1]) if re.fullmatch("h[1-6]", node.name) else 2
                    start_section(line, depth)
                    body.append(line)
                    continue
            if not body or body[-1] != line:
                body.append(line)
                chapter.append(line)
        if not body:
            fallback = text_of(article)
            if fallback:
                body.append(fallback)
                chapter.append(fallback)
        finish()
        if not title or not sections:
            raise ValueError("服务页没有可提取正文或附件")
        info = "正文含原页图片链接" if media_count else None
        self.ctx.emit(Entry("services", url, {
            "标题": title, "分类": category, "来源": "华东师范大学国家大学科技园",
            "文件名": f"{title}.html", "文件格式": "HTML",
            "上传人": "公开来源采集",
            "说明": info, "文档正文": "\n\n".join(body), "章节数": len(sections), "章节": sections,
        }, url, attachments=tuple(dict.fromkeys(originals))))

    def news(self):
        cap = self.options.get("max_news_items") or None
        page_cap = self.options.get("max_news_pages") or None
        seen_urls = set()
        url, page_count = self.config["news_url"], 0
        while url:
            soup = self.html(url)
            links = soup.select(self.selectors["news_link"])
            if not links and page_count == 0:
                raise ValueError("科技园动态列表没有匹配到文章链接")
            for anchor in links:
                article_url = link(url, anchor.get("href"), "kjy.ecnu.edu.cn")
                if not article_url or article_url in seen_urls:
                    continue
                seen_urls.add(article_url)
                context = anchor.find_parent("li")
                evidence_date = read_date(text_of(context))
                try:
                    self.news_article(article_url, evidence_date)
                except Exception as exc:
                    self.ctx.failure(f"园区动态解析异常：{exc}", article_url)
                if cap and len(seen_urls) >= cap:
                    return
            page_count += 1
            if page_cap and page_count >= page_cap:
                return
            next_link = next((a for a in soup.select(self.selectors["next_page"]) if "下一页" in text_of(a)), None)
            next_url = link(url, next_link.get("href"), "kjy.ecnu.edu.cn") if next_link else ""
            if next_url == url or page_count >= 250:
                raise ValueError("科技园动态分页停止推进")
            url = next_url

    def news_article(self, url, evidence_date, raw=None, replay=False):
        raw = raw if raw is not None else self.fetch(url)
        soup = BeautifulSoup(raw, "lxml")
        title = self.title(soup)
        article = self.article(soup)
        evidence_date = read_date(text_of(soup.select_one(self.selectors["article_date"]))) or evidence_date
        fingerprint = digest([PARSER_VERSION, title, evidence_date, text_of(article)])
        with self.ctx.db.engine.connect() as conn:
            seen_hash = conn.scalar(select(news_items.c.content_hash).where(news_items.c.url == url))
        if self.ctx.mode == "incremental" and not replay and not self.ctx.dry_run and seen_hash == fingerprint:
            self.ctx.count("skipped")
            return
        companies = self.company_values(article, title)
        for values in companies:
            name = values["企业名称"]
            key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()
            self.ctx.emit(Entry("companies", f"name:{key}", values | {"数据来源URL": url}, url,
                                evidence_date=evidence_date,
                                attachments=(Original("page_html", url, f"{title}.html", raw),)))
        if not self.ctx.dry_run:
            with self.ctx.db.write() as conn:
                self.ctx.active(conn)
                stmt = insert(news_items).values(url=url, content_hash=fingerprint, company_count=len(companies))
                conn.execute(stmt.on_conflict_do_update(index_elements=["url"],
                    set_={"content_hash": fingerprint, "company_count": len(companies)}))

    def company_values(self, article, title):
        paragraphs = [clean(el.get_text("", strip=False)) for el in article.select("p, li") if text_of(el)]
        if not paragraphs:
            paragraphs = [text_of(article)]
        names = {}
        texts = [(title, 2)]
        for index, paragraph in enumerate(paragraphs):
            context = " ".join(paragraphs[max(0, index - 3):index])
            list_item = re.fullmatch(rf"{COMPANY_NAMES.pattern}(?:\s*[\u3400-\u9fff]{{2,4}})?", paragraph)
            texts.append((paragraph, 2 if list_item and re.search(r"路演|孵化企业|参赛企业", context) else 0))
        texts.extend((text_of(el), 1) for el in article.select("strong, b, a, span"))
        for text, bonus in texts:
            for match in COMPANY_NAMES.finditer(text):
                candidate = COMPANY_PREFIX.split(match.group())[-1]
                if len(candidate) < 6 or len(candidate) > 40 or COMPANY_EXCLUDED.search(candidate):
                    continue
                context = text[max(0, match.start() - 30):match.end() + 40]
                score = bonus + (2 if COMPANY_CONTEXT.search(context) else 0) + (1 if "上海" in candidate else 0)
                names[candidate] = max(score, names.get(candidate, 0))
        results = []
        for name in sorted(names):
            matching = [p for p in paragraphs if name in p]
            if not matching or names[name] < 2 or any(other.endswith(name) and names[other] >= names[name]
                                                       for other in names if other != name):
                continue
            intro = ""
            for paragraph in matching:
                sentences = [s.strip() for s in re.split(r"(?<=[。！？；])", paragraph) if s.strip()]
                for i, sentence in enumerate(sentences):
                    if name not in sentence:
                        continue
                    tail = sentence.partition(name)[2]
                    if COMPANY_INTRO.search(tail) and not re.search(r"主讲|主办|协办|培训|举办|总经理|创始人", tail[:35]):
                        intro = sentence
                        break
                    if i + 1 < len(sentences):
                        adjacent = sentences[i + 1]
                        if re.match(r"^(?:该公司|公司|该企业|企业)(?:目前|现|聚焦|自主)?", adjacent) and COMPANY_INTRO.search(adjacent):
                            intro = adjacent
                            break
                if intro:
                    break
            keywords = [word for word in FIELD_WORDS if word in name or re.search(
                rf"{re.escape(name)}[^。；]{{0,65}}(?:专注|主营|致力于|从事)[^。；]{{0,35}}{word}", intro)]
            values = {"企业名称": name, "技术特色": intro[:450] if intro else None}
            if keywords:
                values["技术领域"] = "、".join(keywords)
            awards = [recognition for recognition in ("高新技术企业", "专精特新") if any(
                re.search(rf"{recognition}[“”\s]*{re.escape(name)}|{re.escape(name)}(?:获评|获认定为|是)(?:园区)?{recognition}", p)
                for p in matching)]
            for index, paragraph in enumerate(paragraphs[:-1]):
                award = paragraphs[index + 1]
                if paragraph == name and re.fullmatch(r"(?:上海市|全国)?(?:特等|[一二三]等|优胜)奖", award):
                    awards.append(award)
            if awards:
                values["资质荣誉"] = "、".join(awards)
            results.append(values)
        return results
