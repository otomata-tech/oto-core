"""Duplication d'une slide et recopie d'une slide vers une autre présentation.

Extrait de `slides_client.py` (découpage par famille d'opérations, surface
publique figée) : les corps sont inchangés. Ce mixin n'est jamais instancié
seul — il est composé dans `SlidesClient`, qui construit `slides_service` et
`drive_service`.
"""


class _CopyMixin:
    """Duplication d'une slide et recopie d'une slide vers une autre présentation."""

    def duplicate_slide(self, presentation_id, slide_id, insertion_index=None):
        """
        Duplicate a slide within the same presentation (uses native API)

        Args:
            presentation_id: ID of the presentation
            slide_id: ID of the slide to duplicate
            insertion_index: Optional position for new slide (default: after source)

        Returns:
            str: Object ID of the duplicated slide

        Example:
            new_slide_id = client.duplicate_slide(pres_id, slide_id)
        """
        request = {
            'duplicateObject': {
                'objectId': slide_id
            }
        }

        if insertion_index is not None:
            request['duplicateObject']['insertionIndex'] = insertion_index

        result = self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': [request]}
        ).execute()

        # Extract new slide ID from response
        if 'replies' in result and len(result['replies']) > 0:
            reply = result['replies'][0]
            if 'duplicateObject' in reply:
                return reply['duplicateObject']['objectId']

        return None

    def copy_slide_to_presentation(self, source_presentation_id, source_slide_id,
                                   target_presentation_id, insertion_index=None,
                                   preserve_layout=True):
        """
        Copy a slide from one presentation to another by recreating its elements

        This method reads the source slide structure and recreates all elements
        in the target presentation. Works across different presentations.

        Args:
            source_presentation_id: ID of source presentation
            source_slide_id: ID of slide to copy
            target_presentation_id: ID of target presentation
            insertion_index: Optional position for new slide
            preserve_layout: Try to preserve original layout (default: True)

        Returns:
            str: Object ID of the new slide

        Note:
            - Supports: text shapes, images, basic positioning
            - May not preserve: complex animations, some advanced features
            - Images need to be accessible (same Drive account or public)

        Example:
            new_slide_id = client.copy_slide_to_presentation(
                source_pres_id, source_slide_id, target_pres_id
            )
        """
        # Read source slide structure
        source_pres = self.get_presentation(source_presentation_id)
        source_slide = None

        for slide in source_pres.get('slides', []):
            if slide['objectId'] == source_slide_id:
                source_slide = slide
                break

        if not source_slide:
            raise ValueError(f"Slide {source_slide_id} not found in source presentation")

        # Determine layout for new slide
        source_layout_id = None
        if preserve_layout and 'slideProperties' in source_slide:
            source_layout_id = source_slide['slideProperties'].get('layoutObjectId')

        # Create new slide in target presentation
        # Use API directly to avoid layout ID being treated as layout name
        requests = []

        if source_layout_id:
            # Check if source layout exists in target (same template)
            target_pres = self.get_presentation(target_presentation_id)
            target_layout_exists = any(
                layout['objectId'] == source_layout_id
                for layout in target_pres.get('layouts', [])
            )

            if target_layout_exists:
                # Use same layout ID directly (same template)
                requests = [{
                    'createSlide': {
                        'slideLayoutReference': {
                            'layoutId': source_layout_id
                        }
                    }
                }]
            else:
                # Try to find matching layout by name (different templates)
                source_layout_name = None
                for layout in source_pres.get('layouts', []):
                    if layout['objectId'] == source_layout_id:
                        source_layout_name = layout.get('layoutProperties', {}).get('name')
                        break

                if source_layout_name:
                    target_layout_id = self.get_layout_id_by_name(target_presentation_id, source_layout_name)
                    if target_layout_id:
                        requests = [{
                            'createSlide': {
                                'slideLayoutReference': {
                                    'layoutId': target_layout_id
                                }
                            }
                        }]

        # Fallback: use BLANK layout
        if not requests:
            requests = [{
                'createSlide': {
                    'slideLayoutReference': {
                        'predefinedLayout': 'BLANK'
                    }
                }
            }]

        if insertion_index is not None:
            requests[0]['createSlide']['insertionIndex'] = insertion_index

        response = self.slides_service.presentations().batchUpdate(
            presentationId=target_presentation_id,
            body={'requests': requests}
        ).execute()

        new_slide_id = response['replies'][0]['createSlide']['objectId']

        # Get the newly created slide with placeholders
        target_pres_initial = self.get_presentation(target_presentation_id)
        new_slide = None
        for slide in target_pres_initial.get('slides', []):
            if slide['objectId'] == new_slide_id:
                new_slide = slide
                break

        # Match and fill placeholders to preserve layout-defined styles
        if new_slide:
            # Collect source placeholders with text
            source_placeholders = []
            for element in source_slide.get('pageElements', []):
                if 'shape' in element:
                    shape = element['shape']
                    if shape.get('placeholder'):
                        text_content = ''
                        if 'text' in shape:
                            for text_elem in shape['text'].get('textElements', []):
                                if 'textRun' in text_elem:
                                    text_content += text_elem['textRun'].get('content', '')

                        source_placeholders.append({
                            'type': shape['placeholder'].get('type'),
                            'index': shape['placeholder'].get('index', 0),
                            'text': text_content
                        })

            # Collect target placeholders
            target_placeholders = []
            for element in new_slide.get('pageElements', []):
                if 'shape' in element:
                    shape = element['shape']
                    if shape.get('placeholder'):
                        target_placeholders.append({
                            'objectId': element['objectId'],
                            'type': shape['placeholder'].get('type'),
                            'index': shape['placeholder'].get('index', 0)
                        })

            # Match placeholders by type and index, then fill with text
            text_requests = []
            for source_ph in source_placeholders:
                if not source_ph['text']:
                    continue

                # Find matching target placeholder
                for target_ph in target_placeholders:
                    if (source_ph['type'] == target_ph['type'] and
                        source_ph['index'] == target_ph['index']):
                        text_requests.append({
                            'insertText': {
                                'objectId': target_ph['objectId'],
                                'text': source_ph['text'],
                                'insertionIndex': 0
                            }
                        })
                        break

            if text_requests:
                self.slides_service.presentations().batchUpdate(
                    presentationId=target_presentation_id,
                    body={'requests': text_requests}
                ).execute()

        # Copy non-placeholder elements from source
        requests = []

        for element in source_slide.get('pageElements', []):
            element_type = None

            # Determine element type
            if 'shape' in element:
                # Skip placeholders (already handled above)
                if element['shape'].get('placeholder'):
                    continue
                element_type = 'shape'
            elif 'image' in element:
                element_type = 'image'
            elif 'table' in element:
                element_type = 'table'
            elif 'line' in element:
                element_type = 'line'
            elif 'video' in element:
                element_type = 'video'
            else:
                # Skip unknown element types
                continue

            # Extract common properties
            transform = element.get('transform', {})
            size = element.get('size', {})

            # Create request based on element type
            if element_type == 'shape':
                shape = element['shape']

                # Create shape
                create_request = {
                    'createShape': {
                        'objectId': None,  # Let API generate ID
                        'shapeType': shape.get('shapeType', 'TEXT_BOX'),
                        'elementProperties': {
                            'pageObjectId': new_slide_id,
                            'size': size,
                            'transform': transform
                        }
                    }
                }
                requests.append(create_request)

                # Store text to add in second pass
                if 'text' in shape:
                    text_content = ''
                    for text_elem in shape['text'].get('textElements', []):
                        if 'textRun' in text_elem:
                            text_content += text_elem['textRun'].get('content', '')

            elif element_type == 'image':
                image = element['image']
                content_url = image.get('contentUrl')

                if content_url:
                    create_request = {
                        'createImage': {
                            'url': content_url,
                            'elementProperties': {
                                'pageObjectId': new_slide_id,
                                'size': size,
                                'transform': transform
                            }
                        }
                    }
                    requests.append(create_request)

        # Execute batch to create non-placeholder elements
        if requests:
            self.slides_service.presentations().batchUpdate(
                presentationId=target_presentation_id,
                body={'requests': requests}
            ).execute()

        # Second pass: Add text content to non-placeholder shapes
        # Get the updated slide
        target_pres = self.get_presentation(target_presentation_id)
        target_slide = None

        for slide in target_pres.get('slides', []):
            if slide['objectId'] == new_slide_id:
                target_slide = slide
                break

        if target_slide:
            text_requests = []
            source_shapes_with_text = []

            # Collect source non-placeholder shapes with text
            for element in source_slide.get('pageElements', []):
                if 'shape' in element:
                    # Skip placeholders
                    if element['shape'].get('placeholder'):
                        continue

                    if 'text' in element['shape']:
                        text_content = ''
                        for text_elem in element['shape']['text'].get('textElements', []):
                            if 'textRun' in text_elem:
                                text_content += text_elem['textRun'].get('content', '')
                        if text_content:
                            source_shapes_with_text.append({
                                'transform': element.get('transform', {}),
                                'text': text_content
                            })

            # Match with target non-placeholder shapes by position
            target_shapes = [
                e for e in target_slide.get('pageElements', [])
                if 'shape' in e and not e['shape'].get('placeholder')
            ]

            for i, source_shape_info in enumerate(source_shapes_with_text):
                if i < len(target_shapes):
                    target_shape_id = target_shapes[i]['objectId']
                    text_requests.append({
                        'insertText': {
                            'objectId': target_shape_id,
                            'text': source_shape_info['text'],
                            'insertionIndex': 0
                        }
                    })

            if text_requests:
                self.slides_service.presentations().batchUpdate(
                    presentationId=target_presentation_id,
                    body={'requests': text_requests}
                ).execute()

        return new_slide_id

