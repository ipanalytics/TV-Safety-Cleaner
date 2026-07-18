# Architecture

TV Observer has four trust boundaries: untrusted TV output, untrusted local imports, private local
state, and authenticated HTTP requests. `adb.py` is the only process boundary and exposes named
read-only methods backed by argument lists. It has no generic command method. `platform.py` combines
properties, packages, and launcher evidence; conflicting or weak evidence yields `unknown`.

`snapshot.py` writes JSON and recovery text to a hidden sibling directory, validates structure and
SHA-256 hashes, then atomically publishes it. `utilities.py` consumes only verified snapshots.
`dns.py` and `observation.py` own separate SQLite tables. `web.py` calls domain operations through
specific forms and never accepts ADB text. The UI is independent from every media-stack service.

Runtime dependencies are Flask and gunicorn. Collection, parsing, JSON, SQLite, archives, hashing,
process execution, paths, and configuration use the Python standard library.
