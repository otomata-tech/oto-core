"""Images : insertion, upload Drive, remplacement de placeholder.

Extrait de `slides_client.py` (découpage par famille d'opérations, surface
publique figée) : les corps sont inchangés. Ce mixin n'est jamais instancié
seul — il est composé dans `SlidesClient`, qui construit `slides_service` et
`drive_service`.
"""

from pathlib import Path


class _ImagesMixin:
    """Images : insertion, upload Drive, remplacement de placeholder."""

    def insert_image(self, presentation_id, slide_id, image_url, x, y, width, height):
        """
        Insert an image into a slide

        Args:
            presentation_id: ID of the presentation
            slide_id: ID of the slide
            image_url: URL of the image (must be publicly accessible)
            x, y: Position in EMU (1 pt = 12700 EMU)
            width, height: Size in EMU

        Returns:
            str: Object ID of the created image
        """
        import uuid
        object_id = f'image_{uuid.uuid4().hex[:8]}'

        requests = [{
            'createImage': {
                'objectId': object_id,
                'url': image_url,
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
        }]

        self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': requests}
        ).execute()

        return object_id

    def upload_image_to_drive(self, image_path, folder_id=None):
        """
        Upload a local image file to Google Drive

        Args:
            image_path: Path to the local image file
            folder_id: Optional folder ID to upload to

        Returns:
            str: Publicly accessible URL of the uploaded image
        """
        from googleapiclient.http import MediaFileUpload
        import mimetypes

        file_name = Path(image_path).name
        mime_type, _ = mimetypes.guess_type(image_path)
        if not mime_type:
            mime_type = 'image/png'

        file_metadata = {'name': file_name}
        if folder_id:
            file_metadata['parents'] = [folder_id]

        media = MediaFileUpload(image_path, mimetype=mime_type)
        file = self.drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webContentLink',
            supportsAllDrives=True
        ).execute()

        # Make the file publicly readable
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        self.drive_service.permissions().create(
            fileId=file['id'],
            body=permission,
            supportsAllDrives=True
        ).execute()

        # Return the direct download URL
        file_id = file['id']
        return f"https://drive.google.com/uc?export=view&id={file_id}"

    def replace_image_placeholder(self, presentation_id, image_object_id, image_url, replace_method='CENTER_INSIDE'):
        """
        Replace an image placeholder with actual image content

        Args:
            presentation_id: ID of the presentation
            image_object_id: Object ID of the image element to replace
            image_url: URL of the image (must be publicly accessible)
            replace_method: How to fit the image ('CENTER_INSIDE' or 'CENTER_CROP')

        Returns:
            API response
        """
        requests = [{
            'replaceImage': {
                'imageObjectId': image_object_id,
                'url': image_url,
                'imageReplaceMethod': replace_method
            }
        }]

        return self.slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={'requests': requests}
        ).execute()

    def get_image_placeholders_in_slide(self, presentation_id, slide_id):
        """
        Get all image elements/placeholders in a slide

        Args:
            presentation_id: ID of the presentation
            slide_id: ID of the slide

        Returns:
            list: List of image object IDs
        """
        presentation = self.get_presentation(presentation_id)

        for slide in presentation.get('slides', []):
            if slide['objectId'] == slide_id:
                image_ids = []
                for elem in slide.get('pageElements', []):
                    if 'image' in elem:
                        image_ids.append(elem['objectId'])
                return image_ids

        return []

