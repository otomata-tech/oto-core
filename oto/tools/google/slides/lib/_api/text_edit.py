"""Lecture et édition du texte des formes d'une slide.

Extrait de `slides_client.py` (découpage par famille d'opérations, surface
publique figée) : les corps sont inchangés. Ce mixin n'est jamais instancié
seul — il est composé dans `SlidesClient`, qui construit `slides_service` et
`drive_service`.
"""


class _TextEditMixin:
    """Lecture et édition du texte des formes d'une slide."""

    def get_text_content(self, presentation_id, slide_id, object_id):
        """
        Get text content from a shape or text box

        Args:
            presentation_id: ID of presentation
            slide_id: ID of slide
            object_id: ID of shape/text box

        Returns:
            str: Text content or None if not found
        """
        presentation = self.get_presentation(presentation_id)

        for slide in presentation.get('slides', []):
            if slide['objectId'] == slide_id:
                for elem in slide.get('pageElements', []):
                    if elem['objectId'] == object_id:
                        if 'shape' in elem and 'text' in elem['shape']:
                            text = ''
                            for text_elem in elem['shape']['text'].get('textElements', []):
                                if 'textRun' in text_elem:
                                    text += text_elem['textRun'].get('content', '')
                            return text
        return None

    def get_text_objects_in_slide(self, presentation_id, slide_id):
        """
        Get all text objects (shapes with text) in a slide

        Args:
            presentation_id: ID of presentation
            slide_id: ID of slide

        Returns:
            list: List of dicts with objectId, text content, shapeType, and position info
        """
        presentation = self.get_presentation(presentation_id)

        for slide in presentation.get('slides', []):
            if slide['objectId'] == slide_id:
                text_objects = []

                for elem in slide.get('pageElements', []):
                    if 'shape' in elem:
                        shape = elem['shape']
                        if 'text' in shape:
                            # Extract text content
                            text = ''
                            for text_elem in shape['text'].get('textElements', []):
                                if 'textRun' in text_elem:
                                    text += text_elem['textRun'].get('content', '')

                            text_objects.append({
                                'objectId': elem['objectId'],
                                'shapeType': shape.get('shapeType'),
                                'text': text,
                                'transform': elem.get('transform'),
                                'size': elem.get('size')
                            })

                return text_objects

        return []

    def _edit_text_preserve_style(self, presentation_id, object_id, new_text):
        """
        Internal method: Edit text while preserving formatting

        Preserves the style (size, bold, color, etc.) of the first text run
        """
        # Get current shape and extract style
        presentation = self.get_presentation(presentation_id)

        # Find the shape
        shape_element = None
        for slide in presentation.get('slides', []):
            for element in slide.get('pageElements', []):
                if element['objectId'] == object_id:
                    shape_element = element
                    break
            if shape_element:
                break

        if not shape_element or 'shape' not in shape_element:
            raise ValueError(f"Shape {object_id} not found")

        shape = shape_element['shape']

        # Extract text style from first text run
        text_style = None
        paragraph_style = None

        if 'text' in shape:
            text_content = shape['text']
            text_elements = text_content.get('textElements', [])

            # Get text style from first text run
            for text_elem in text_elements:
                if 'textRun' in text_elem:
                    text_run = text_elem['textRun']
                    text_style = text_run.get('style', {})
                    break

            # Get paragraph style
            for text_elem in text_elements:
                if 'paragraphMarker' in text_elem:
                    paragraph_style = text_elem['paragraphMarker'].get('style', {})
                    break

        # Build requests: delete, insert, apply styles
        requests = [
            {
                'deleteText': {
                    'objectId': object_id,
                    'textRange': {'type': 'ALL'}
                }
            },
            {
                'insertText': {
                    'objectId': object_id,
                    'text': new_text,
                    'insertionIndex': 0
                }
            }
        ]

        # Apply text style if found
        if text_style:
            requests.append({
                'updateTextStyle': {
                    'objectId': object_id,
                    'style': text_style,
                    'textRange': {'type': 'ALL'},
                    'fields': '*'
                }
            })

        # Apply paragraph style if found
        if paragraph_style:
            requests.append({
                'updateParagraphStyle': {
                    'objectId': object_id,
                    'style': paragraph_style,
                    'textRange': {'type': 'ALL'},
                    'fields': '*'
                }
            })

        return self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': requests}
        ).execute()

    def edit_text(self, presentation_id, object_id, new_text,
                  start_index=None, end_index=None, preserve_style=True):
        """
        Edit text in an existing shape or text box

        Args:
            presentation_id: ID of the presentation
            object_id: ID of the shape/text box
            new_text: New text content
            start_index: Optional start position (None = replace all)
            end_index: Optional end position (None = replace all)
            preserve_style: Preserve formatting (size, bold, color) - default True

        Returns:
            dict: API response

        Example:
            # Replace all text in a text box (preserving style)
            client.edit_text(pres_id, obj_id, "New content")

            # Replace without preserving style
            client.edit_text(pres_id, obj_id, "New content", preserve_style=False)

            # Replace specific range
            client.edit_text(pres_id, obj_id, "Insert", start_index=5, end_index=10)
        """
        # If replacing all text and preserving style, use optimized method
        if start_index is None and end_index is None and preserve_style:
            return self._edit_text_preserve_style(presentation_id, object_id, new_text)

        # Otherwise use traditional delete+insert (loses style)
        requests = []

        if start_index is None and end_index is None:
            # Replace all text
            requests = [
                {
                    'deleteText': {
                        'objectId': object_id,
                        'textRange': {'type': 'ALL'}
                    }
                },
                {
                    'insertText': {
                        'objectId': object_id,
                        'text': new_text,
                        'insertionIndex': 0
                    }
                }
            ]
        else:
            # Replace specific range
            if start_index is None:
                start_index = 0

            # If end_index not provided, we need to delete to end
            # For simplicity, if end_index is None, we replace from start_index to end
            if end_index is None:
                # Delete from start to end, then insert
                requests = [
                    {
                        'deleteText': {
                            'objectId': object_id,
                            'textRange': {
                                'type': 'FROM_START_INDEX',
                                'startIndex': start_index
                            }
                        }
                    },
                    {
                        'insertText': {
                            'objectId': object_id,
                            'text': new_text,
                            'insertionIndex': start_index
                        }
                    }
                ]
            else:
                # Delete specific range, then insert
                requests = [
                    {
                        'deleteText': {
                            'objectId': object_id,
                            'textRange': {
                                'type': 'FIXED_RANGE',
                                'startIndex': start_index,
                                'endIndex': end_index
                            }
                        }
                    },
                    {
                        'insertText': {
                            'objectId': object_id,
                            'text': new_text,
                            'insertionIndex': start_index
                        }
                    }
                ]

        return self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': requests}
        ).execute()

    def replace_all_text(self, presentation_id, find_text, replace_text,
                        match_case=False, page_object_ids=None):
        """
        Replace all occurrences of text in presentation (global find/replace)

        Useful for replacing placeholders like {{company}}, {{date}}, etc.

        Args:
            presentation_id: ID of presentation
            find_text: Text to find
            replace_text: Replacement text
            match_case: Whether to match case (default: False)
            page_object_ids: Optional list of slide IDs to limit search

        Returns:
            dict: API response with occurrencesChanged count

        Example:
            # Replace all placeholders
            result = client.replace_all_text(pres_id, '{{company}}', 'Acme Corp')
            print(f"Replaced {result['replies'][0]['replaceAllText']['occurrencesChanged']} occurrences")
        """
        request = {
            'replaceAllText': {
                'containsText': {
                    'text': find_text,
                    'matchCase': match_case
                },
                'replaceText': replace_text
            }
        }

        if page_object_ids:
            request['replaceAllText']['pageObjectIds'] = page_object_ids

        return self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': [request]}
        ).execute()

