"""Cycle de vie d'une présentation et de son fichier Drive.

Extrait de `slides_client.py` (découpage par famille d'opérations, surface
publique figée) : les corps sont inchangés. Ce mixin n'est jamais instancié
seul — il est composé dans `SlidesClient`, qui construit `slides_service` et
`drive_service`.
"""

import io
import tempfile

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


class _PresentationsMixin:
    """Cycle de vie d'une présentation et de son fichier Drive."""

    def create_presentation(self, title, folder_id=None, template_id=None):
        """
        Create a new presentation, optionally from a template

        Args:
            title: Title of the presentation
            folder_id: Optional Google Drive folder ID to create in
            template_id: Optional presentation ID to use as template (copies theme)

        Returns:
            dict: Presentation resource with id, title, etc.
        """
        if template_id:
            # Copy template presentation
            copy_metadata = {
                'name': title
            }
            if folder_id:
                copy_metadata['parents'] = [folder_id]

            copied_file = self.drive_service.files().copy(
                fileId=template_id,
                body=copy_metadata,
                supportsAllDrives=True,
                fields='id'
            ).execute()

            presentation_id = copied_file['id']
            presentation = self.slides_service.presentations().get(
                presentationId=presentation_id).execute()

        elif folder_id:
            # Create file in Drive first, then use Slides API
            file_metadata = {
                'name': title,
                'mimeType': 'application/vnd.google-apps.presentation',
                'parents': [folder_id]
            }
            file = self.drive_service.files().create(
                body=file_metadata,
                fields='id',
                supportsAllDrives=True
            ).execute()
            presentation_id = file['id']
            # Get presentation details
            presentation = self.slides_service.presentations().get(
                presentationId=presentation_id).execute()
        else:
            body = {'title': title}
            presentation = self.slides_service.presentations().create(
                body=body).execute()
        return presentation

    def get_presentation(self, presentation_id):
        """Get presentation details"""
        return self.slides_service.presentations().get(
            presentationId=presentation_id).execute()

    def move_to_folder(self, file_id, folder_id):
        """
        Move a file to a specific folder in Drive

        Args:
            file_id: ID of the file
            folder_id: ID of the target folder
        """
        # Get current parents
        file = self.drive_service.files().get(
            fileId=file_id, fields='parents').execute()
        previous_parents = ",".join(file.get('parents', []))

        # Move to new folder
        self.drive_service.files().update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()

    def create_folder(self, folder_name, parent_folder_id=None):
        """
        Create a folder in Google Drive

        Args:
            folder_name: Name of the folder
            parent_folder_id: Optional parent folder ID

        Returns:
            str: ID of the created folder
        """
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_folder_id:
            file_metadata['parents'] = [parent_folder_id]

        folder = self.drive_service.files().create(
            body=file_metadata,
            fields='id',
            supportsAllDrives=True
        ).execute()

        return folder['id']

    def share_presentation(self, presentation_id, role='reader', type='anyone'):
        """
        Share a presentation

        Args:
            presentation_id: ID of the presentation
            role: Permission role (reader, writer, commenter)
            type: Permission type (user, group, domain, anyone)
        """
        permission = {
            'type': type,
            'role': role
        }

        self.drive_service.permissions().create(
            fileId=presentation_id,
            body=permission,
            supportsAllDrives=True
        ).execute()

    def get_presentation_url(self, presentation_id):
        """Get the URL to view/edit a presentation"""
        return f"https://docs.google.com/presentation/d/{presentation_id}/edit"

    def get_slide_ids(self, presentation_id):
        """
        Get list of all slide IDs in presentation

        Args:
            presentation_id: ID of presentation

        Returns:
            list: List of slide object IDs in order
        """
        presentation = self.get_presentation(presentation_id)
        return [slide['objectId'] for slide in presentation.get('slides', [])]

    def convert_pptx_to_native(self, pptx_id, name, folder_id=None):
        """
        Convert a .pptx file already in Drive into a native Google Slides file.

        `drive.files().copy()` ne convertit pas le mimeType, donc on télécharge
        puis on ré-upload en spécifiant `mimeType=application/vnd.google-apps.presentation`
        — Drive fait la conversion à l'upload.

        Args:
            pptx_id: Drive file ID of the source .pptx
            name: Name of the resulting Google Slides file
            folder_id: Optional Drive folder ID to place the result in

        Returns:
            str: presentationId of the newly created Google Slides file
        """
        req = self.drive_service.files().get_media(fileId=pptx_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)

        with tempfile.NamedTemporaryFile(suffix='.pptx', delete=False) as tmp:
            tmp.write(buf.read())
            tmp_path = tmp.name

        media = MediaFileUpload(
            tmp_path,
            mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        )
        body = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.presentation',
        }
        if folder_id:
            body['parents'] = [folder_id]
        f = self.drive_service.files().create(
            body=body, media_body=media, fields='id', supportsAllDrives=True,
        ).execute()
        return f['id']

    def export_pdf(self, presentation_id, output_path):
        """Exporte la présentation en PDF localement (via Drive export)."""
        data = self.drive_service.files().export(
            fileId=presentation_id, mimeType='application/pdf'
        ).execute()
        with open(output_path, 'wb') as f:
            f.write(data)
        return output_path
