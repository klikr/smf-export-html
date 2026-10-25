import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from export_forum import Board, ExportError, ForumExporter, format_post, layout, load_config, page_links, safe_url


class FakeCursor:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params):
        if "`smf_settings`" in sql:
            self.rows = [{"value": "0"}]
        elif "FROM `smf_boards` b" in sql:
            self.rows = [{"id_board": 2, "id_cat": 3, "id_parent": 0, "name": "Forum", "description": "Board description"}]
        elif "SELECT DISTINCT c.id_cat" in sql:
            self.rows = [{"id_cat": 3, "name": "Category", "description": "", "cat_order": 1}]
        elif "FROM `smf_membergroups`" in sql:
            self.rows = [
                {"group_name": "Newbie", "min_posts": 0},
                {"group_name": "Member", "min_posts": 5},
            ]
        elif "COUNT(*) AS total FROM `smf_topics` t" in sql:
            self.rows = [{"total": 1}]
        elif "SELECT t.id_topic," in sql:
            self.rows = [{
                "id_topic": 7,
                "reply_count": 1,
                "num_views": 12,
                "is_sticky": 0,
                "subject": 'A &quot;quoted&quot; topic',
                "starter_name": "Alice",
            }]
        elif "COUNT(*) AS total FROM `smf_messages`" in sql:
            self.rows = [{"total": 2}]
        elif "SELECT m.subject FROM `smf_topics`" in sql:
            self.rows = [{"subject": "A topic"}]
        elif "SELECT m.id_msg" in sql:
            post_number = int(params[2]) + 1
            self.rows = [{
                "id_msg": post_number,
                "poster_time": 1609459200,
                "poster_name": "Alice",
                "body": f"Post {post_number}",
                "display_name": "Alice",
                "member_posts": 8,
                "id_member": 10,
            }]
        elif "SELECT id_member, avatar" in sql:
            self.rows = [{"id_member": 10, "avatar": ""}]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def cursor(self, dictionary=False):
        return FakeCursor()


class ExportFormattingTests(unittest.TestCase):
    def test_post_rank_uses_highest_threshold_not_exceeding_post_count(self):
        exporter = ForumExporter(
            Namespace(db_prefix="smf_", output=Path("."), media_dir="media", logo_path=None,
                      attachment_dirs={}, media_sources={}, topics_per_page=1, posts_per_page=1),
            FakeConnection(),
        )
        exporter.post_ranks = [(0, "Newbie"), (5, "Member"), (20, "Veteran")]

        self.assertEqual(exporter.post_rank(0), "Newbie")
        self.assertEqual(exporter.post_rank(5), "Member")
        self.assertEqual(exporter.post_rank(19), "Member")
        self.assertEqual(exporter.post_rank(20), "Veteran")

    def test_external_avatar_is_kept_as_validated_external_url(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "archive"
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())

            def avatar_query(sql, params=()):
                if "SELECT id_member, avatar" in sql:
                    return [{"id_member": 10, "avatar": "https://avatars.example/user.png"}]
                return []

            exporter.query = avatar_query
            exporter.copy_avatars([10])

            self.assertEqual(exporter.avatar_cache[10], "https://avatars.example/user.png")

    def test_post_escapes_html_and_formats_basic_bbcode(self):
        rendered = format_post("<script>alert(1)</script> [b]hello[/b]\nworld")
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("<strong>hello</strong>", rendered)
        self.assertIn("<br>", rendered)
        self.assertNotIn("<script>", rendered)

    def test_post_decodes_smf_entities_and_breaks_once(self):
        rendered = format_post("He&#39;ll read it<br>next line &amp; smile")
        self.assertIn("He'll read it<br>\nnext line &amp; smile", rendered)
        self.assertNotIn("&#39;", rendered)
        self.assertNotIn("&amp;lt;br", rendered)

    def test_post_escapes_html_text_delimiters_but_keeps_apostrophes_readable(self):
        rendered = format_post("apostrophe ' ampersand & less < greater > quote \"")
        self.assertIn("apostrophe ' ampersand &amp; less &lt; greater &gt; quote \"", rendered)
        self.assertNotIn("&#39;", rendered)

    def test_post_formats_smf_quote_attributes(self):
        rendered = format_post("[quote author=Alice link=msg=123 date=456]<br>Quoted text[/quote]")
        self.assertIn("<blockquote><cite>Quote from: Alice</cite>", rendered)
        self.assertIn("Quoted text", rendered)
        self.assertNotIn("[quote author=", rendered)

    def test_post_escapes_raw_html_and_preserves_nobbc_content(self):
        rendered = format_post("[html]<b>not executable</b>[/html] [nobbc][i]literal[/i][/nobbc]")
        self.assertIn("&lt;b&gt;not executable&lt;/b&gt;", rendered)
        self.assertIn("[i]literal[/i]", rendered)
        self.assertNotIn("<b>not executable</b>", rendered)
        self.assertNotIn("<em>literal</em>", rendered)

    def test_post_formats_size_bbcode_with_safe_css_sizes(self):
        rendered = format_post("[size=11pt]Small text[/size] [size=large]Large text[/size] [size=5]Big[/size]")
        self.assertIn('<span style="font-size:11pt">Small text</span>', rendered)
        self.assertIn('<span style="font-size:large">Large text</span>', rendered)
        self.assertIn('<span style="font-size:2em">Big</span>', rendered)
        self.assertNotIn("[size=", rendered)

    def test_post_formats_smf_list_item_markers(self):
        rendered = format_post("[list][*]First[*]Second[/list]")
        self.assertIn("<ul><li>First</li><li>Second</li></ul>", rendered)

    def test_post_formats_common_smF_layout_and_inline_tags(self):
        rendered = format_post(
            "[center][color=#123abc]Hello[/color][/center] "
            "[sub]2[/sub] [sup]nd[/sup] [bdo=rtl]text[/bdo]"
        )
        self.assertIn('<div class="centertext"><span style="color:#123abc">Hello</span></div>', rendered)
        self.assertIn("<sub>2</sub>", rendered)
        self.assertIn("<sup>nd</sup>", rendered)
        self.assertIn('<bdo dir="rtl">text</bdo>', rendered)

    def test_post_does_not_render_unsafe_size_bbcode_as_css(self):
        rendered = format_post('[size=11pt; color:red]Text[/size]')
        self.assertIn("[size=11pt; color:red]", rendered)
        self.assertNotIn("style=", rendered)

    def test_post_renders_img_bbcodes_with_external_urls_and_parameters(self):
        rendered = format_post(
            '[img width=320 height=180 alt="Forum image" title="Example"]'
            "https://images.example/photo.png[/img]"
        )
        self.assertIn(
            '<img class="post-image" src="https://images.example/photo.png" alt="Forum image" '
            'title="Example" width="320" height="180">',
            rendered,
        )
        self.assertNotIn("[img", rendered)
        self.assertNotIn("[/img]", rendered)

    def test_post_keeps_unsafe_img_url_as_escaped_text(self):
        rendered = format_post("[img]javascript:alert(1)[/img]")
        self.assertIn("[img]javascript:alert(1)[/img]", rendered)
        self.assertNotIn('<img class="post-image"', rendered)

    def test_post_formats_standard_link_variants_and_email_equals(self):
        rendered = format_post(
            "[iurl=https://forum.example/topic]topic[/iurl] "
            "[ftp]files.example/archive.zip[/ftp] "
            "[email=member@example.com]mail me[/email]"
        )
        self.assertIn('href="https://forum.example/topic"', rendered)
        self.assertIn('href="ftp://files.example/archive.zip"', rendered)
        self.assertIn('href="mailto:member@example.com"', rendered)
        self.assertEqual(rendered.count('target="_blank"'), 3)
        self.assertIn('rel="nofollow noopener noreferrer"', rendered)
        self.assertIn('rel="noopener noreferrer"', rendered)

    def test_post_formats_float_youtube_and_language_code_bbcodes(self):
        rendered = format_post(
            "[float=right max=80%]Aside[/float] "
            "[youtube]dQw4w9WgXcQ[/youtube] "
            "[code=python]print('safe')[/code]"
        )
        self.assertIn(
            '<div class="bbc_float floatright" style="float:right;max-width:80%">Aside</div>',
            rendered,
        )
        self.assertIn('href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"', rendered)
        self.assertIn('target="_blank" rel="nofollow noopener noreferrer"', rendered)
        self.assertIn("<pre><code>print('safe')</code></pre>", rendered)

    def test_post_validates_links(self):
        rendered = format_post("[url]https://example.com/a?b=1&c=2[/url] [url=javascript:alert(1)]bad[/url]")
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', rendered)
        self.assertIn('target="_blank" rel="nofollow noopener noreferrer"', rendered)
        self.assertIn("[url=javascript:alert(1)]", rendered)
        self.assertNotIn('href="javascript:', rendered)

    def test_post_formats_quoted_entity_encoded_url_target(self):
        rendered = format_post(
            '[url=&amp;quot;https://www.arenaband.co.uk/product/lifian-tour-mmxxii-digipack/&amp;quot;]'
            "https://www.arenaband.co.uk/product/lifian-tour-mmxxii-digipack/[/url]"
        )

        self.assertIn(
            '<a href="https://www.arenaband.co.uk/product/lifian-tour-mmxxii-digipack/" '
            'target="_blank" rel="nofollow noopener noreferrer">',
            rendered,
        )
        self.assertNotIn("[url=", rendered)

    def test_post_renders_code_without_interpreting_markup(self):
        rendered = format_post("[code][b]not bold[/b] <x>[/code]")
        self.assertIn("<pre><code>[b]not bold[/b] &lt;x&gt;</code></pre>", rendered)
        self.assertNotIn("<strong>", rendered)

    def test_post_links_to_copied_attachments(self):
        rendered = format_post(
            "See [attach]42[/attach]",
            {42: {"href": "../../attachments/42.download", "name": "report.pdf"}},
        )
        self.assertIn('href="../../attachments/42.download"', rendered)
        self.assertIn('download="report.pdf"', rendered)
        self.assertIn(">report.pdf</a>", rendered)

    def test_safe_url_only_allows_supported_schemes(self):
        self.assertEqual(safe_url("https://example.com/path"), "https://example.com/path")
        self.assertIsNone(safe_url("javascript:alert(1)"))
        self.assertIsNone(safe_url("//example.com/path"))

    def test_pagination_marks_current_page_and_links_other_pages(self):
        rendered = page_links(2, 3, lambda page: f"page-{page}.html")
        self.assertIn('aria-current="page">2</span>', rendered)
        self.assertIn('href="page-1.html"', rendered)
        self.assertIn('href="page-3.html"', rendered)
        self.assertEqual(page_links(1, 1, lambda page: "index.html"), "")

    def test_large_pagination_shows_page_window_and_page_jump(self):
        rendered = page_links(10, 30, lambda page: "index.html" if page == 1 else f"page-{page}.html")

        self.assertIn('href="index.html">First</a>', rendered)
        self.assertIn('href="page-9.html" rel="prev">Previous</a>', rendered)
        self.assertIn('aria-current="page">10</span>', rendered)
        self.assertIn('href="page-11.html"', rendered)
        self.assertIn('href="page-12.html"', rendered)
        self.assertIn('href="page-11.html" rel="next">Next</a>', rendered)
        self.assertIn('href="page-30.html">Last</a>', rendered)
        self.assertIn('max="30"', rendered)
        self.assertIn('value="10"', rendered)
        self.assertIn("Go</button>", rendered)

    def test_large_pagination_handles_first_and_last_pages(self):
        first = page_links(1, 6, lambda page: "index.html" if page == 1 else f"page-{page}.html")
        last = page_links(6, 6, lambda page: "index.html" if page == 1 else f"page-{page}.html")

        self.assertIn('aria-disabled="true">Previous</span>', first)
        self.assertIn('href="page-2.html" rel="next">Next</a>', first)
        self.assertIn('href="page-5.html" rel="prev">Previous</a>', last)
        self.assertIn('aria-disabled="true">Next</span>', last)

    def test_layout_uses_relative_home_links(self):
        root_page = layout("Archive", "", "assets/style.css")
        nested_page = layout("Forum", "", "../../assets/style.css", home_href="../../index.html")
        self.assertIn('href="index.html">Forum archive', root_page)
        self.assertIn('href="../../index.html">Forum archive', nested_page)
        self.assertIn('src="../../assets/archive.js"', nested_page)

    def test_layout_includes_relative_logo_in_header(self):
        rendered = layout("Archive", "", "assets/style.css", logo_href="media/forum-logo.png")
        self.assertIn('<img src="media/forum-logo.png" alt="">', rendered)
        self.assertIn('class="site-brand"', rendered)

    def test_export_writes_linked_forum_and_paginated_post_pages(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.guest_boards = lambda: [Board(2, 3, 0, "Forum", "Board description")]
            exporter.copy_avatars = lambda member_ids: exporter.avatar_cache.update(
                {member_id: "https://avatars.example/user.png" for member_id in member_ids}
            )
            exporter.categories = lambda board_ids: [
                {"id_cat": 3, "name": "Category", "description": "", "cat_order": 1}
            ]
            exporter.run()

            start_page = (output / "index.html").read_text(encoding="utf-8")
            forum_page = (output / "boards/2/index.html").read_text(encoding="utf-8")
            post_page_two = (output / "topics/7/page-2.html").read_text(encoding="utf-8")
            stylesheet = (output / "assets/style.css").read_text(encoding="utf-8")
            script = (output / "assets/archive.js").read_text(encoding="utf-8")
            self.assertIn('href="boards/2/index.html"', start_page)
            self.assertIn('href="../../topics/7/index.html"', forum_page)
            self.assertIn('A "quoted" topic</a>', forum_page)
            self.assertNotIn("&amp;quot;", forum_page)
            self.assertNotIn("&quot;", forum_page)
            self.assertIn('href="index.html"', post_page_two)
            self.assertIn("Post 2", post_page_two)
            self.assertIn('href="../../assets/style.css"', post_page_two)
            self.assertIn(">Member</span> · 8 posts", post_page_two)
            self.assertIn('src="https://avatars.example/user.png"', post_page_two)
            self.assertIn('<time class="muted post-time" datetime="2021-01-01T00:00:00+00:00" title="UTC">2021-01-01 00:00</time>', post_page_two)
            self.assertNotIn("Posted", post_page_two)
            self.assertNotIn("UTC</time>", post_page_two)
            self.assertIn(".avatar,.avatar-placeholder{width:48px;height:48px;border-radius:0;", stylesheet)
            self.assertIn('src="../../assets/archive.js"', post_page_two)
            self.assertIn("page-${page}.html", script)

    def test_export_writes_empty_forum_page(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={},
                topics_per_page=25,
                posts_per_page=20,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.guest_boards = lambda: [Board(2, 3, 0, "Forum", "Board description")]
            exporter.categories = lambda board_ids: [
                {"id_cat": 3, "name": "Category", "description": "", "cat_order": 1}
            ]
            exporter.topic_count = lambda board_id: 0
            original_query = exporter.query
            exporter.query = lambda sql, params=(): [] if "SELECT t.id_topic," in sql else original_query(sql, params)
            exporter.run()

            forum_page = (output / "boards/2/index.html").read_text(encoding="utf-8")
            self.assertIn("No public topics in this forum.", forum_page)

    def test_export_reports_topic_progress_with_total(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.total_topics = 2
            exporter.topic_count = lambda board_id: 2
            exporter.write_topic = lambda topic_id, board: None

            def topic_query(sql, params=()):
                topic_id = int(params[2]) + 10
                return [{
                    "id_topic": topic_id,
                    "reply_count": 0,
                    "num_views": 0,
                    "is_sticky": 0,
                    "subject": f"Topic {topic_id}",
                    "starter_name": "Alice",
                }]

            exporter.query = topic_query
            progress = StringIO()
            with redirect_stdout(progress):
                exporter.write_board(Board(2, 3, 0, "Forum", "Board description"))

            self.assertEqual(
                progress.getvalue().splitlines(),
                ["Exporting topic 1 of 2", "Exporting topic 2 of 2"],
            )

    def test_load_config_resolves_paths_relative_to_config_file(self):
        with TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_file = config_dir / "config.json"
            config_file.write_text(json.dumps({
                "database": {"host": "db.example", "name": "forum", "user": "reader", "password": "secret"},
                "output_dir": "archive",
                "logo_path": "../Logo1.png",
                "attachment_dirs": {"1": "../attachments"},
                "media_sources": {
                    "smileys": {
                        "path": "../Smileys",
                        "url_prefix": "https://forum.example/Smileys/",
                    }
                },
            }), encoding="utf-8")

            config = load_config(config_file)
            self.assertEqual(config.db_host, "db.example")
            self.assertEqual(config.output, config_dir / "archive")
            self.assertEqual(config.logo_path, (config_dir / "../Logo1.png").resolve())
            self.assertEqual(config.attachment_dirs["1"], (config_dir / "../attachments").resolve())
            self.assertEqual(config.media_sources["smileys"]["path"], str((config_dir / "../Smileys").resolve()))
            self.assertEqual(config.media_sources["smileys"]["url_prefix"], "https://forum.example/Smileys/")

    def test_load_config_rejects_missing_database_fields(self):
        with TemporaryDirectory() as temporary:
            config_file = Path(temporary) / "config.json"
            config_file.write_text('{"database":{"host":"localhost","name":"forum"}}', encoding="utf-8")
            with self.assertRaisesRegex(ExportError, "database.user"):
                load_config(config_file)

    def test_copies_media_sources_into_single_media_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "smileys"
            nested = source / "fugue"
            nested.mkdir(parents=True)
            (nested / "smile.png").write_bytes(b"image-data")
            args = Namespace(
                db_prefix="smf_",
                output=root / "archive",
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={
                    "smileys": {
                        "path": source,
                        "url_prefix": "https://forum.example/Smileys/",
                    }
                },
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.ensure_output()

            copied = root / "archive/media/smileys/fugue/smile.png"
            self.assertEqual(copied.read_bytes(), b"image-data")
            self.assertEqual(
                exporter.resolve_local_image("https://forum.example/Smileys/fugue/smile.png"),
                "../../media/smileys/fugue/smile.png",
            )
            self.assertIsNone(exporter.resolve_local_image("https://external.example/image.png"))

    def test_copies_configured_logo_into_archive_media_and_links_it(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            logo = root / "Logo1.png"
            logo.write_bytes(b"forum-logo")
            output = root / "archive"
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=logo,
                attachment_dirs={},
                media_sources={},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.ensure_output()
            exporter.write_index([], {}, {})

            self.assertEqual((output / "media/forum-logo.png").read_bytes(), b"forum-logo")
            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('<img src="media/forum-logo.png" alt="">', index)

    def test_copies_attachments_into_configured_media_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment_source = root / "attachments"
            attachment_source.mkdir()
            (attachment_source / f"{42}_{'a' * 40}.dat").write_bytes(b"attachment")
            output = root / "archive"
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="all-assets",
                logo_path=None,
                attachment_dirs={"1": attachment_source},
                media_sources={},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.ensure_output()
            exporter.query = lambda sql, params=(): [{
                "id_attach": 42,
                "id_msg": 7,
                "id_folder": 1,
                "filename": "picture.jpg",
                "file_hash": "a" * 40,
            }]

            copied = exporter.copy_attachments([7])
            target = output / "all-assets/attachments/42-picture.jpg"
            self.assertEqual(target.read_bytes(), b"attachment")
            self.assertEqual(copied[7][0]["href"], "all-assets/attachments/42-picture.jpg")
            self.assertEqual(copied[7][0]["name"], "picture.jpg")

    def test_attachment_export_preserves_file_extension_and_encodes_url_name(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            attachment_source = root / "attachments"
            attachment_source.mkdir()
            (attachment_source / f"{43}_{'b' * 40}.dat").write_bytes(b"pdf-bytes")
            output = root / "archive"
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=None,
                attachment_dirs={"1": attachment_source},
                media_sources={},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.ensure_output()
            exporter.query = lambda sql, params=(): [{
                "id_attach": 43,
                "id_msg": 7,
                "id_folder": 1,
                "filename": "Quarterly report (final).pdf",
                "file_hash": "b" * 40,
            }]

            copied = exporter.copy_attachments([7])
            self.assertEqual(
                copied[7][0]["href"],
                "media/attachments/43-Quarterly report (final).pdf",
            )
            rendered = format_post("Download [attach]43[/attach]", {
                43: {
                    "href": "../../media/attachments/43-Quarterly report (final).pdf",
                    "name": "Quarterly report (final).pdf",
                }
            })
            self.assertIn(
                'href="../../media/attachments/43-Quarterly%20report%20%28final%29.pdf"',
                rendered,
            )
            self.assertIn('download="Quarterly report (final).pdf"', rendered)

    def test_copies_attachment_avatar_from_custom_avatar_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom_avatar_dir = root / "custom_avatar"
            custom_avatar_dir.mkdir()
            (custom_avatar_dir / "avatar_199.png").write_bytes(b"avatar")
            output = root / "archive"
            args = Namespace(
                db_prefix="smf_",
                output=output,
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={"custom_avatars": {"path": custom_avatar_dir, "url_prefix": ""}},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.ensure_output()

            def avatar_query(sql, params=()):
                if "SELECT id_member, avatar" in sql:
                    return [{"id_member": 10, "avatar": ""}]
                if "attachment_type = 1" in sql:
                    return [{
                        "id_member": 10,
                        "id_attach": 199,
                        "id_folder": 1,
                        "filename": "avatar_199.png",
                        "file_hash": "a" * 40,
                    }]
                return []

            exporter.query = avatar_query
            exporter.copy_avatars([10])

            copied_avatar = output / "media/avatars/10.png"
            self.assertEqual(copied_avatar.read_bytes(), b"avatar")
            self.assertEqual(exporter.avatar_cache[10], "media/avatars/10.png")

    def test_post_renders_smileys_and_local_images(self):
        rendered = format_post(
            "Hello :) [img]https://forum.example/image.png[/img]",
            image_resolver=lambda url: "../../media/images/image.png" if url.endswith("image.png") else None,
            smileys={":)": "../../media/smileys/fugue/smile.png"},
        )
        self.assertIn('<img class="smiley" src="../../media/smileys/fugue/smile.png" alt=":)">', rendered)
        self.assertIn('<img class="post-image" src="../../media/images/image.png" alt="">', rendered)

    def test_loads_default_smiley_set_from_configured_media(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            smiley_root = root / "smileys"
            smiley_file = smiley_root / "fugue/smile.png"
            smiley_file.parent.mkdir(parents=True)
            smiley_file.write_bytes(b"smiley")
            args = Namespace(
                db_prefix="smf_",
                output=root / "archive",
                media_dir="media",
                logo_path=None,
                attachment_dirs={},
                media_sources={"smileys": {"path": smiley_root, "url_prefix": ""}},
                topics_per_page=1,
                posts_per_page=1,
            )
            exporter = ForumExporter(args, FakeConnection())
            exporter.query = lambda sql, params=(): (
                [{"value": "fugue"}]
                if "smiley_sets_default" in sql
                else [{"code": ":)", "filename": "smile.png"}]
            )

            exporter.load_smileys()
            self.assertEqual(exporter.smileys, {":)": "../../media/smileys/fugue/smile.png"})


if __name__ == "__main__":
    unittest.main()
