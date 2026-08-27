"""Layouts du master : résolution, création de slides, génération en lot.

Extrait de `slides_client.py` (découpage par famille d'opérations, surface
publique figée) : les corps sont inchangés. Ce mixin n'est jamais instancié
seul — il est composé dans `SlidesClient`, qui construit `slides_service` et
`drive_service`.
"""

from ..markup import parse_bold_markdown


class _LayoutsMixin:
    """Layouts du master : résolution, création de slides, génération en lot."""

    def get_layout_id_by_name(self, presentation_id, layout_name):
        """
        Get layout object ID by predefined layout name

        Args:
            presentation_id: ID of the presentation
            layout_name: Name like 'TITLE_AND_BODY', 'TITLE_ONLY', etc.

        Returns:
            str: Layout object ID, or None if not found
        """
        presentation = self.get_presentation(presentation_id)

        # Build a mapping of available layouts
        layouts_by_name = {}
        default_layout_id = None

        for layout in presentation.get('layouts', []):
            props = layout.get('layoutProperties', {})
            display_name = props.get('displayName', '')
            name = props.get('name', '')
            object_id = layout['objectId']

            # Map by API name (always available)
            layouts_by_name[name] = object_id

            # Also map by display name for easier lookup
            if display_name:
                layouts_by_name[display_name] = object_id

            # Remember DEFAULT or first layout as fallback
            if display_name == 'DEFAULT' or name == 'DEFAULT':
                default_layout_id = object_id
            elif default_layout_id is None:
                default_layout_id = object_id

        # Try to find the requested layout
        if layout_name in layouts_by_name:
            return layouts_by_name[layout_name]

        # Fallback: use DEFAULT for TITLE_AND_BODY if not available
        if layout_name == 'TITLE_AND_BODY' and 'DEFAULT' in layouts_by_name:
            return layouts_by_name['DEFAULT']

        # Last resort: return default
        return default_layout_id

    def add_slide(self, presentation_id, layout='BLANK', insertion_index=None):
        """
        Add a new slide to presentation

        Args:
            presentation_id: ID of the presentation
            layout: Layout type (BLANK, TITLE_ONLY, TITLE, SECTION_HEADER, etc.)
            insertion_index: Position to insert (None = end)

        Returns:
            str: Object ID of the created slide
        """
        # Try to find layout by name first (for templates)
        layout_id = self.get_layout_id_by_name(presentation_id, layout)

        if layout_id:
            # Use layout object ID
            requests = [{
                'createSlide': {
                    'slideLayoutReference': {
                        'layoutId': layout_id
                    }
                }
            }]
        else:
            # Use predefined layout name (for new presentations)
            requests = [{
                'createSlide': {
                    'slideLayoutReference': {
                        'predefinedLayout': layout
                    }
                }
            }]

        if insertion_index is not None:
            requests[0]['createSlide']['insertionIndex'] = insertion_index

        response = self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': requests}
        ).execute()

        return response['replies'][0]['createSlide']['objectId']

    def get_layouts_map(self, presentation_id):
        """
        Return {layout_name: {'objectId': ..., 'placeholders': [(type, index), ...]}}.

        Useful pour découvrir les layouts d'un master importé avant de les
        utiliser via `build_from_layouts`.
        """
        pres = self.get_presentation(presentation_id)
        out = {}
        for layout in pres.get('layouts', []):
            props = layout.get('layoutProperties', {})
            name = props.get('name', '')
            placeholders = []
            for el in layout.get('pageElements', []):
                if 'shape' in el and el['shape'].get('placeholder'):
                    ph = el['shape']['placeholder']
                    placeholders.append((ph.get('type'), ph.get('index', 0)))
            out[name] = {
                'objectId': layout['objectId'],
                'placeholders': placeholders,
                'displayName': props.get('displayName', ''),
            }
        return out

    def clear_all_slides(self, presentation_id, keepalive_layout_id=None):
        """
        Supprime toutes les slides existantes en gardant une slide temporaire
        (l'API Slides refuse une présentation à 0 slide).

        Args:
            presentation_id: ID de la présentation
            keepalive_layout_id: layoutId d'une slide temporaire à insérer en queue.
                Si None, prend le premier layout disponible.

        Returns:
            str: objectId de la slide temporaire keepalive
                 (à supprimer après avoir créé tes vraies slides)
        """
        pres = self.slides_service.presentations().get(
            presentationId=presentation_id, fields='slides(objectId),layouts(objectId)'
        ).execute()
        existing = [s['objectId'] for s in pres.get('slides', [])]

        if keepalive_layout_id is None:
            layouts = pres.get('layouts', [])
            if not layouts:
                raise ValueError("Aucun layout disponible pour le keepalive")
            keepalive_layout_id = layouts[0]['objectId']

        keepalive_id = 'tmp_keepalive_oto'
        # Si déjà présent (re-run), réutiliser
        if keepalive_id in existing:
            others = [s for s in existing if s != keepalive_id]
            if others:
                self.slides_service.presentations().batchUpdate(
                    presentationId=presentation_id,
                    body={'requests': [{'deleteObject': {'objectId': s}} for s in others]},
                ).execute()
            return keepalive_id

        requests = [{
            'createSlide': {
                'objectId': keepalive_id,
                'slideLayoutReference': {'layoutId': keepalive_layout_id},
            }
        }]
        for s in existing:
            requests.append({'deleteObject': {'objectId': s}})
        self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id, body={'requests': requests}
        ).execute()
        return keepalive_id

    def build_from_layouts(self, presentation_id, slides_def,
                           override_body_bold=True, parse_markdown_bold=True):
        """
        Génère une série de slides à partir d'une liste de définitions, en
        peu de batchUpdate (= robuste face au quota 60 writes/min/user).

        Approche :
        1. 1 batchUpdate `createSlide` avec `placeholderIdMappings` → objectIds
           prédictibles.
        2. 1 batchUpdate `insertText` + `updateTextStyle` (bold) sur tous les
           placeholders à remplir.

        Args:
            presentation_id: ID de la présentation cible (déjà nettoyée si besoin)
            slides_def: liste de tuples `(slide_id, layout_id, fills)` où `fills`
                est un dict `{(placeholder_type, placeholder_index): text}`.
                `slide_id` doit faire ≥ 5 caractères (contrainte API).
            override_body_bold: si True (défaut), force `bold:False` sur tous les
                placeholders BODY avant d'appliquer les ranges bold issues du
                markdown. Utile quand le master rend BODY en gras par défaut
                (cas du template Otomata) — sinon `**bold**` est invisible.
            parse_markdown_bold: si True (défaut), parse les segments `**…**`
                du texte et applique `updateTextStyle bold:True` dessus.

        Returns:
            list[str]: la liste des objectIds des slides créées
        """
        # Phase 1 — création des slides + placeholderIdMappings
        create_reqs = []
        for idx, (slide_id, layout_id, fills) in enumerate(slides_def):
            if len(slide_id) < 5:
                raise ValueError(
                    f"slide_id {slide_id!r} doit faire ≥ 5 caractères "
                    f"(contrainte Slides API)"
                )
            ph_mappings = [
                {
                    'layoutPlaceholder': {'type': ph_type, 'index': ph_index},
                    'objectId': f'{slide_id}_{ph_type}_{ph_index}',
                }
                for (ph_type, ph_index) in fills.keys()
            ]
            create_reqs.append({
                'createSlide': {
                    'objectId': slide_id,
                    'insertionIndex': idx,
                    'slideLayoutReference': {'layoutId': layout_id},
                    'placeholderIdMappings': ph_mappings,
                }
            })

        if create_reqs:
            self.slides_service.presentations().batchUpdate(
                presentationId=presentation_id, body={'requests': create_reqs}
            ).execute()

        # Phase 2 — insertText + updateTextStyle
        fill_reqs = []
        for slide_id, _layout_id, fills in slides_def:
            for (ph_type, ph_index), text in fills.items():
                obj_id = f'{slide_id}_{ph_type}_{ph_index}'
                if parse_markdown_bold:
                    clean, bolds = parse_bold_markdown(text)
                else:
                    clean, bolds = text, []
                if not clean:
                    continue
                fill_reqs.append({
                    'insertText': {
                        'objectId': obj_id,
                        'text': clean,
                        'insertionIndex': 0,
                    }
                })
                # Casser l'héritage bold du master (cas Otomata) sur les BODY
                if override_body_bold and ph_type == 'BODY':
                    fill_reqs.append({
                        'updateTextStyle': {
                            'objectId': obj_id,
                            'textRange': {'type': 'ALL'},
                            'style': {'bold': False},
                            'fields': 'bold',
                        }
                    })
                # Appliquer les ranges bold issues du markdown
                for start, end in bolds:
                    fill_reqs.append({
                        'updateTextStyle': {
                            'objectId': obj_id,
                            'textRange': {
                                'type': 'FIXED_RANGE',
                                'startIndex': start,
                                'endIndex': end,
                            },
                            'style': {'bold': True},
                            'fields': 'bold',
                        }
                    })

        if fill_reqs:
            self.slides_service.presentations().batchUpdate(
                presentationId=presentation_id, body={'requests': fill_reqs}
            ).execute()

        return [slide_id for slide_id, _, _ in slides_def]

