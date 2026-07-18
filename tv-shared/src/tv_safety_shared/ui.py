from __future__ import annotations

from pathlib import Path
from typing import cast

from flask import Blueprint, Flask
from jinja2 import ChoiceLoader, FileSystemLoader


def install_shared_ui(app: Flask) -> None:
    package_root = Path(__file__).resolve().parent
    shared_templates = FileSystemLoader(str(package_root / "templates"))
    if app.jinja_loader is None:
        app.jinja_loader = shared_templates
    else:
        app.jinja_loader = cast(
            FileSystemLoader,
            ChoiceLoader((shared_templates, app.jinja_loader)),
        )
    app.register_blueprint(
        Blueprint(
            "suite_ui",
            __name__,
            static_folder=str(package_root / "static"),
            static_url_path="/suite-static",
        )
    )
