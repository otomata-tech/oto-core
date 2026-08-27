"""Création de zones de texte et application de styles.

Extrait de `slides_client.py` (découpage par famille d'opérations, surface
publique figée) : les corps sont inchangés. Ce mixin n'est jamais instancié
seul — il est composé dans `SlidesClient`, qui construit `slides_service` et
`drive_service`.
"""

from ..markup import _hex_to_rgb


class _TextStyleMixin:
    """Création de zones de texte et application de styles."""

    def add_text_box(self, presentation_id, slide_id, text, x, y, width, height):
        """
        Add a text box to a slide

        Args:
            presentation_id: ID of the presentation
            slide_id: ID of the slide
            text: Text content
            x, y, width, height: Position and size in EMU (1 pt = 12700 EMU)

        Returns:
            str: Object ID of the created text box
        """
        object_id = f'textbox_{slide_id}_{len(text)}'

        requests = [
            {
                'createShape': {
                    'objectId': object_id,
                    'shapeType': 'TEXT_BOX',
                    'elementProperties': {
                        'pageObjectId': slide_id,
                        'size': {
                            'width': {'magnitude': width, 'unit': 'EMU'},
                            'height': {'magnitude': height, 'unit': 'EMU'}
                        },
                        'transform': {
                            'scaleX': 1,
                            'scaleY': 1,
                            'translateX': x,
                            'translateY': y,
                            'unit': 'EMU'
                        }
                    }
                }
            },
            {
                'insertText': {
                    'objectId': object_id,
                    'text': text,
                    'insertionIndex': 0
                }
            }
        ]

        self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': requests}
        ).execute()

        return object_id

    def set_text_style(self, presentation_id, object_id, font_size=None, bold=None):
        """
        Set text style for an object

        Args:
            presentation_id: ID of the presentation
            object_id: ID of the text object
            font_size: Font size in points
            bold: Whether text should be bold
        """
        requests = []

        if font_size:
            requests.append({
                'updateTextStyle': {
                    'objectId': object_id,
                    'style': {
                        'fontSize': {
                            'magnitude': font_size,
                            'unit': 'PT'
                        }
                    },
                    'fields': 'fontSize'
                }
            })

        if bold is not None:
            requests.append({
                'updateTextStyle': {
                    'objectId': object_id,
                    'style': {'bold': bold},
                    'fields': 'bold'
                }
            })

        if requests:
            self.slides_service.presentations().batchUpdate(
                presentationId=presentation_id,
                body={'requests': requests}
            ).execute()

    def format_text_range(self, presentation_id, object_id, start_index, end_index,
                         bold=None, italic=None, underline=None, link=None,
                         fg_color=None, bg_color=None):
        """
        Apply formatting to a specific text range

        Args:
            presentation_id: ID of the presentation
            object_id: ID of the text object
            start_index: Start position (0-based)
            end_index: End position (exclusive)
            bold: Boolean for bold
            italic: Boolean for italic
            underline: Boolean for underline
            link: URL string for hyperlink
            fg_color: Foreground color (hex like '#FF0000' or RGB dict)
            bg_color: Background color (hex like '#FFFF00' or RGB dict)
        """
        style = {}
        fields = []

        if bold is not None:
            style['bold'] = bold
            fields.append('bold')

        if italic is not None:
            style['italic'] = italic
            fields.append('italic')

        if underline is not None:
            style['underline'] = underline
            fields.append('underline')

        if link:
            style['link'] = {'url': link}
            fields.append('link')

        if fg_color:
            rgb = _hex_to_rgb(fg_color) if isinstance(fg_color, str) else fg_color
            style['foregroundColor'] = {
                'opaqueColor': {'rgbColor': rgb}
            }
            fields.append('foregroundColor')

        if bg_color:
            rgb = _hex_to_rgb(bg_color) if isinstance(bg_color, str) else bg_color
            style['backgroundColor'] = {
                'opaqueColor': {'rgbColor': rgb}
            }
            fields.append('backgroundColor')

        if not fields:
            return  # Nothing to do

        request = {
            'updateTextStyle': {
                'objectId': object_id,
                'textRange': {
                    'type': 'FIXED_RANGE',
                    'startIndex': start_index,
                    'endIndex': end_index
                },
                'style': style,
                'fields': ','.join(fields)
            }
        }

        self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': [request]}
        ).execute()

