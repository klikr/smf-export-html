# Static SMF forum archive

This utility exports guest-visible SMF 2.1 forum content into paginated static HTML pages. It reads the database directly and does not crawl the live site or write to the forum database.

> **Disclaimer:** All code and documentation in this project were created with AI assistance. Review and test them before use.
>
> **Test environment disclaimer:** The project was tested with Ubuntu 22.04, MariaDB 10.6.23, Python 3.10.12, and SMF 2.1.8. Other environments have been and most probably will not be verified.
>
> Functionality is not guaranteed. The maintainers accept no responsibility for any issues arising from its use.
>
> Always ensure you have a working backup before running the exporter or making changes to your setup. Use a database account with read-only access for the export.

## Example output

The result is a simple, sleek collection of static web pages, with pagination
for large topics. With the example settings, the exporter creates an archive
under `site/`:

```text
site/
├── index.html
├── assets/
│   ├── style.css
│   └── archive.js
├── boards/
│   └── <board-id>/
│       ├── index.html
│       └── page-2.html
├── topics/
│   └── <topic-id>/
│       ├── index.html
│       └── page-2.html
└── media/
    ├── forum-logo.png
    ├── attachments/
    ├── avatars/
    ├── smileys/
    ├── custom_avatars/
    └── theme_images/
```

Board and topic folders contain additional `page-N.html` files when needed.
The logo and media subfolders are included when configured or when there are
files to export; the media-source subfolder names correspond to the names in
`media_sources`. The HTML, CSS, and JavaScript can be served as ordinary static
files.

## Configure

Install the database connector:

```sh
python3 -m pip install -r requirements.txt
```

Copy `config.example.json` to `config.json`, then edit the database login and local SMF paths:

```sh
cp config.example.json config.json
```

The config is JSON. Example:

```json
{
  "database": {
    "host": "localhost",
    "port": 3306,
    "name": "smf",
    "user": "archive_reader",
    "password": "change-this",
    "prefix": "smf_"
  },
  "output_dir": "site",
  "media_dir": "media",
  "logo_path": "/absolute/path/to/SMF/Logo1.png",
  "attachment_dirs": {
    "1": "../attachments"
  },
  "media_sources": {
    "smileys": {
      "path": "../Smileys",
      "url_prefix": "https://example.com/Smileys/"
    },
    "avatars": {
      "path": "../avatars",
      "url_prefix": "https://example.com/avatars/"
    },
    "custom_avatars": {
      "path": "../custom_avatar",
      "url_prefix": "https://example.com/custom_avatar/"
    },
    "theme_images": {
      "path": "../Themes/default/images",
      "url_prefix": "https://example.com/Themes/default/images/"
    }
  },
  "topics_per_page": 25,
  "posts_per_page": 20
}
```

All paths may be absolute or relative to the config file. `attachment_dirs` maps SMF attachment folder IDs to their upload directories; add an entry for every configured upload directory. `media_sources` maps named source directories to the archive's single `site/media/<name>/` tree. Every configured media source directory is copied recursively, preserving its subdirectory structure; symlinks are skipped. Include any additional image or media folders you want in the archive.

Set `logo_path` to the forum's local logo image file. The exporter copies it into `site/media/forum-logo.<extension>` and displays it in each page header. This must be a filesystem path accessible to the machine running the exporter; the script will report an error if the configured file is missing. The forum logo file was not present in this SMF source folder, so replace the example path with the actual location on your server.

Set each `url_prefix` to the URL path prefix that SMF uses for that local directory. The exporter uses the mapping to turn matching `[img]...[/img]` references into local links. If an image URL does not match a configured local media source, valid HTTP(S) image URLs remain external; the exporter does not download them. Smileys are read from SMF's database and rendered using the configured local smiley-set files.

Post text is entity-decoded once before BBCode rendering, then escaped for HTML text nodes. The exporter handles SMF's standard emphasis, size/color/font, alignment, quote/list/table, link/email, code, image, attachment, and related display tags. External HTTP(S), FTP, and email links open in a new tab; archive navigation remains within the current tab. Image tags accept width, height, alt, and title parameters; image URLs must be configured local media or valid HTTP(S) URLs. Raw `[html]` content is escaped rather than executed, and legacy video tags are rendered as links instead of active embeds. Custom BBCodes added by forum modifications are not automatically exported. `<`, `>`, and `&` are escaped; quotes and apostrophes are retained as readable text because they do not need escaping in text nodes. HTML attributes (such as URLs, image paths, and download names) escape both quote types as well. This prevents post text from becoming markup while avoiding visible entities such as `&#39;`. Stored `<br>` variants are converted into actual line breaks before rendering.

Database credentials are read only from the config file; the script does not use environment variables or command-line database credentials. A read-only database account is recommended. `config.json` is git-ignored so credentials are not accidentally committed.

## Run

From this directory:

```sh
python3 export_forum.py
```

Specify another config file with `--config /path/to/config.json`. Output defaults to `export/site`; page sizes default to 25 topics and 20 posts per page.

During export, the command prints progress as each topic is generated, for example `Exporting topic 12 of 400`.
Forum and topic pages with more than five pages show first/previous/next/last controls, a window of nearby page numbers, and a page-number field for direct navigation. Pages with five or fewer continue to show every page number.

SMF uses a hashed `.dat` filename for attachment storage, but that `.dat` suffix is only SMF's storage name: its contents are the uploaded file bytes. The exporter copies those bytes unchanged into `site/media/attachments/` using an archive name like `42-original-name.pdf`, preserving the original extension so browsers and tools can recognize the file type. HTML links use the original filename in the `download` attribute, so the browser saves it with its forum filename. Custom uploaded avatars are also copied under `site/media/avatars/`; validated external HTTP(S) avatars remain external images. Avatars are displayed square. Post metadata shows each member's post-count rank before their post count, and timestamps display in UTC with the timezone label available on hover. Configured smileys, stock avatars, custom avatar files, theme images, and any additional media sources all live under `site/media/`.

Private messages, boards not visible to guests, unapproved topics/posts, email addresses, and IP addresses are not exported. Static files do not enforce SMF logins or permissions; keep the archive private unless you intend it to be public. The script overwrites generated files but does not remove stale/unrelated files in the output directory, so check available disk space before exporting a large forum.
