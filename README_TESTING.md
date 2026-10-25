# Exporter tests

`test_export_forum.py` is the project's standard-library `unittest` suite for
`export_forum.py`. It checks the formatter and exporter behavior without
connecting to a real SMF database or writing to a real archive. Database
responses are supplied by test doubles, and filesystem tests use temporary
directories that are removed automatically.

## Run the tests

From the project directory, run the full suite with either command:

```sh
python3 -m unittest -v test_export_forum
```

or:

```sh
python3 test_export_forum.py
```

The tests use Python's built-in `unittest` module. They do not need a database,
`config.json`, or the MySQL connector used by the exporter to connect to SMF.

To run one test, use its class and method name:

```sh
python3 -m unittest -v \
  test_export_forum.ExportFormattingTests.test_post_escapes_html_and_formats_basic_bbcode
```

To discover all matching test files in the project directory:

```sh
python3 -m unittest discover -v
```

## What the suite covers

- Post formatting, including common SMF BBCode, HTML escaping, entity decoding,
  links, images, smileys, and attachments.
- URL validation and safe handling of image and link targets.
- Page layout, relative asset links, pagination controls, and navigation.
- Exported forum and topic pages, including empty forums, post metadata, and
  progress output.
- Configuration validation and resolution of paths relative to the config
  file.
- Copying configured media, the forum logo, attachments, and custom avatars;
  loading the default smiley set.
- Post-count rank selection and external avatar handling.

The suite exercises these behaviors with representative fixtures; it is not an
integration test against a live SMF installation. In particular, it does not
verify database connectivity, permissions on a real forum, or the contents of
your production media directories.
