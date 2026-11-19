"""Validation errors of the store, theme, category and property-definition forms, shown in the page
language. Their services (app/services/store_admin.py, category_admin.py) return error **codes**
``(code, params)``; the message is ``admin.form.error.<code>`` in app/locales/*.json. The keys are
written out literally below so tests/test_i18n.py finds them (like the product and shipping forms)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, TypeVar

from app.i18n import Translator
from app.services.store_admin import FieldError

FormT = TypeVar("FormT")


def form_error_text(t: Translator, error: FieldError) -> str:
    """One error in the page language."""
    code, params = error
    messages = {
        # stores
        "store_slug_format": lambda: t("admin.form.error.store_slug_format", **params),
        "slug_reserved": lambda: t("admin.form.error.slug_reserved", **params),
        "store_slug_taken": lambda: t("admin.form.error.store_slug_taken"),
        "currency_format": lambda: t("admin.form.error.currency_format"),
        "locale_invalid": lambda: t("admin.form.error.locale_invalid"),
        # theme: the color is named with the theme page's own label
        "color_format": lambda: t("admin.form.error.color_format", label=t(f"admin.theme.label.{params['key']}")),
        # shared by stores / categories
        "slug_required": lambda: t("admin.form.error.slug_required"),
        "name_required": lambda: t("admin.form.error.name_required"),
        "name_too_long": lambda: t("admin.form.error.name_too_long", **params),
        # categories
        "slug_format": lambda: t("admin.form.error.slug_format", **params),
        "category_slug_taken": lambda: t("admin.form.error.category_slug_taken"),
        "description_too_long": lambda: t("admin.form.error.description_too_long", **params),
        "parent_cycle": lambda: t("admin.form.error.parent_cycle"),
        "parent_invalid": lambda: t("admin.form.error.parent_invalid"),
        "has_products": lambda: t("admin.form.error.has_products", **params),
        "has_subcategories": lambda: t("admin.form.error.has_subcategories", **params),
        # categories / properties
        "sort_format": lambda: t("admin.form.error.sort_format"),
        "sort_range": lambda: t("admin.form.error.sort_range", **params),
        # property definitions
        "key_required": lambda: t("admin.form.error.key_required"),
        "key_format": lambda: t("admin.form.error.key_format", **params),
        "key_taken": lambda: t("admin.form.error.key_taken"),
        "label_required": lambda: t("admin.form.error.label_required"),
        "label_too_long": lambda: t("admin.form.error.label_too_long", **params),
        "type_invalid": lambda: t("admin.form.error.type_invalid"),
        "type_locked": lambda: t("admin.form.error.type_locked"),
        "options_required": lambda: t("admin.form.error.options_required"),
        "options_too_many": lambda: t("admin.form.error.options_too_many", **params),
        "option_too_long": lambda: t("admin.form.error.option_too_long", **params),
        "option_duplicate": lambda: t("admin.form.error.option_duplicate", **params),
        "ref_category_invalid": lambda: t("admin.form.error.ref_category_invalid"),
    }
    return messages[code]()


def shown(form: FormT, t: Translator) -> FormT:
    """A copy of ``form`` whose ``errors`` are texts in the page language, for the template. The
    form itself keeps the codes."""
    errors: dict[str, Any] = getattr(form, "errors")
    return replace(form, errors={name: form_error_text(t, error) for name, error in errors.items()})
