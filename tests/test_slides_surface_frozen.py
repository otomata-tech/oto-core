"""La surface de `SlidesClient` est un CONTRAT.

`oto.tools.google.slides.commands` (façade CLI) et les générateurs de decks
importent `SlidesClient` depuis `oto.tools.google.slides.lib.slides_client` :
le chemin, les méthodes et leurs défauts sont figés.

Posé avec le découpage du client par famille d'opérations (2026-08-27,
1 516 lignes → `slides_client.py` + `markup.py` + `_api/*`) : prouver qu'aucune
surface n'est perdue, plutôt que l'affirmer.

⚠️ Ajouter une méthode fait échouer ce test : c'est voulu — mets à jour la
constante en connaissance de cause.
"""
import inspect

from oto.tools.google.slides.lib import slides_client as mod

# {nom de membre: signature str, ou None pour un attribut de classe}
EXPECTED_MEMBERS = {
    'SCOPES': None,
    '_edit_text_preserve_style': '(self, presentation_id, object_id, new_text)',
    'add_slide': "(self, presentation_id, layout='BLANK', insertion_index=None)",
    'add_text_box': '(self, presentation_id, slide_id, text, x, y, width, height)',
    'build_from_layouts': '(self, presentation_id, slides_def, override_body_bold=True, parse_markdown_bold=True)',
    'clear_all_slides': '(self, presentation_id, keepalive_layout_id=None)',
    'convert_pptx_to_native': '(self, pptx_id, name, folder_id=None)',
    'copy_slide_to_presentation': '(self, source_presentation_id, source_slide_id, target_presentation_id, insertion_index=None, preserve_layout=True)',
    'create_folder': '(self, folder_name, parent_folder_id=None)',
    'create_presentation': '(self, title, folder_id=None, template_id=None)',
    'duplicate_slide': '(self, presentation_id, slide_id, insertion_index=None)',
    'edit_text': '(self, presentation_id, object_id, new_text, start_index=None, end_index=None, preserve_style=True)',
    'export_pdf': '(self, presentation_id, output_path)',
    'format_text_range': '(self, presentation_id, object_id, start_index, end_index, bold=None, italic=None, underline=None, link=None, fg_color=None, bg_color=None)',
    'get_image_placeholders_in_slide': '(self, presentation_id, slide_id)',
    'get_layout_id_by_name': '(self, presentation_id, layout_name)',
    'get_layouts_map': '(self, presentation_id)',
    'get_presentation': '(self, presentation_id)',
    'get_presentation_url': '(self, presentation_id)',
    'get_slide_ids': '(self, presentation_id)',
    'get_text_content': '(self, presentation_id, slide_id, object_id)',
    'get_text_objects_in_slide': '(self, presentation_id, slide_id)',
    'insert_image': '(self, presentation_id, slide_id, image_url, x, y, width, height)',
    'move_to_folder': '(self, file_id, folder_id)',
    'replace_all_text': '(self, presentation_id, find_text, replace_text, match_case=False, page_object_ids=None)',
    'replace_image_placeholder': "(self, presentation_id, image_object_id, image_url, replace_method='CENTER_INSIDE')",
    'set_text_style': '(self, presentation_id, object_id, font_size=None, bold=None)',
    'share_presentation': "(self, presentation_id, role='reader', type='anyone')",
    'upload_image_to_drive': '(self, image_path, folder_id=None)',
}


def _members(cls):
    out = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("__"):
            continue
        try:
            out[name] = str(inspect.signature(member))
        except (TypeError, ValueError):
            out[name] = None
    return out


def test_membres_et_signatures_inchanges():
    got = _members(mod.SlidesClient)
    assert set(got) == set(EXPECTED_MEMBERS), (
        f"membres ajoutés: {sorted(set(got) - set(EXPECTED_MEMBERS))} / "
        f"disparus: {sorted(set(EXPECTED_MEMBERS) - set(got))}")
    for name, sig in sorted(EXPECTED_MEMBERS.items()):
        assert got[name] == sig, f"signature changée pour {name}: {got[name]}"


def test_helpers_toujours_importables_depuis_slides_client():
    """`parse_bold_markdown` / `_hex_to_rgb` vivent dans `markup.py` depuis le
    découpage, mais restent importables par leur chemin historique."""
    assert mod.parse_bold_markdown("a **b** c") == ("a b c", [(2, 3)])
    assert mod._hex_to_rgb("#FF0000") == {"red": 1.0, "green": 0.0, "blue": 0.0}
    assert set(mod.__all__) == {"SlidesClient", "parse_bold_markdown",
                                "_hex_to_rgb"}


def test_aucun_module_du_connecteur_ne_depasse_500_lignes():
    """Le seuil qui a motivé le découpage se garde tout seul (audit 27/08)."""
    import pathlib
    lib_dir = pathlib.Path(mod.__file__).parent
    trop_gros = {
        p.name: len(p.read_text(encoding="utf-8").splitlines())
        for p in sorted(lib_dir.rglob("*.py"))
        if len(p.read_text(encoding="utf-8").splitlines()) >= 500
    }
    assert not trop_gros, trop_gros
