import logging
from collections import defaultdict

from imdb import IMDb
from django.apps import apps
from django.conf import settings
from django.utils import timezone

import app
from app.models import MediaTypes, Sources, Status
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError

logger = logging.getLogger(__name__)


def importer(user_id, user, mode):
    """Import watchlist and ratings from IMDB."""
    imdb_importer = IMDBImporter(user_id, user, mode)
    return imdb_importer.import_data()


class IMDBImporter:
    """Class to handle importing user data from IMDB."""

    def __init__(self, user_id, user, mode):
        """Initialize the importer with IMDB user ID, user, and mode.

        Args:
            user_id (str): IMDB user ID to import from
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.user_id = user_id
        self.user = user
        self.mode = mode
        self.warnings = []

        # Initialize IMDb instance
        self.imdb = IMDb()

        # Track existing media for "new" mode
        self.existing_media = helpers.get_existing_media(user)

        # Track media IDs to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))

        # Track bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        logger.info(
            "Initialized IMDB importer for user %s with mode %s",
            user_id,
            mode,
        )

    def import_data(self):
        """Import user data from IMDB."""
        try:
            # Get user information to validate the user ID
            user_info = self.imdb.get_person(self.user_id)
            if not user_info:
                msg = f"User with ID {self.user_id} not found on IMDB."
                raise MediaImportError(msg)

            logger.info("Starting IMDB import for user %s", self.user_id)

            # Import watchlist
            self._import_watchlist()

            # Import ratings
            self._import_ratings()

            helpers.cleanup_existing_media(self.to_delete, self.user)
            helpers.bulk_create_media(self.bulk_media, self.user)

            imported_counts = {
                media_type: len(media_list)
                for media_type, media_list in self.bulk_media.items()
            }

            deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
            return imported_counts, deduplicated_messages

        except Exception as error:
            if isinstance(error, MediaImportError):
                raise
            msg = f"Error accessing IMDB user {self.user_id}: {error}"
            raise MediaImportError(msg) from error

    def _import_watchlist(self):
        """Import watchlist from IMDB."""
        try:
            logger.info("Importing watchlist for user %s", self.user_id)
            watchlist = self.imdb.get_user_watchlist(self.user_id)

            if not watchlist:
                logger.info("No watchlist found for user %s", self.user_id)
                return

            for item in watchlist:
                try:
                    self._process_item(item, Status.PLANNING.value)
                except MediaImportError as error:
                    self.warnings.append(str(error))
                except Exception as error:
                    title = item.get("title", "Unknown")
                    msg = f"Error processing watchlist item: {title}"
                    raise MediaImportUnexpectedError(msg) from error

        except Exception as error:
            if isinstance(error, MediaImportError):
                raise
            msg = f"Error importing watchlist: {error}"
            raise MediaImportError(msg) from error

    def _import_ratings(self):
        """Import ratings from IMDB."""
        try:
            logger.info("Importing ratings for user %s", self.user_id)
            ratings = self.imdb.get_user_ratings(self.user_id)

            if not ratings:
                logger.info("No ratings found for user %s", self.user_id)
                return

            for item in ratings:
                try:
                    # Get the rating value
                    rating = item.get("rating", 0)
                    # Convert IMDB rating (1-10) to our scale (1-10)
                    score = float(rating) if rating else None

                    self._process_item(item, Status.COMPLETED.value, score)
                except MediaImportError as error:
                    self.warnings.append(str(error))
                except Exception as error:
                    title = item.get("title", "Unknown")
                    msg = f"Error processing rating item: {title}"
                    raise MediaImportUnexpectedError(msg) from error

        except Exception as error:
            if isinstance(error, MediaImportError):
                raise
            msg = f"Error importing ratings: {error}"
            raise MediaImportError(msg) from error

    def _process_item(self, item, status, score=None):
        """Process a single IMDB item."""
        # Extract IMDB ID
        imdb_id = item.movieID
        if not imdb_id:
            title = item.get("title", "Unknown")
            msg = f"{title}: No IMDB ID found"
            raise MediaImportError(msg)

        # Determine media type based on IMDB kind
        kind = item.get("kind", "movie")
        if kind in ["tv series", "tv mini series"]:
            media_type = MediaTypes.TV.value
        elif kind == "movie":
            media_type = MediaTypes.MOVIE.value
        else:
            title = item.get("title", "Unknown")
            msg = f"{title}: Unsupported media type '{kind}'"
            raise MediaImportError(msg)

        # Get TMDB ID using IMDB ID
        tmdb_id = self._get_tmdb_id(imdb_id, media_type)
        if not tmdb_id:
            title = item.get("title", "Unknown")
            msg = f"{title}: Could not find TMDB ID for IMDB ID {imdb_id}"
            raise MediaImportError(msg)

        # Check if we should process this entry based on mode
        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            media_type,
            Sources.TMDB.value,
            str(tmdb_id),
            self.mode,
        ):
            return

        # Get metadata from TMDB
        metadata = self._get_metadata(media_type, tmdb_id, item.get("title", "Unknown"))
        if not metadata:
            title = item.get("title", "Unknown")
            msg = f"{title}: Could not get metadata for TMDB ID {tmdb_id}"
            raise MediaImportError(msg)

        # Create Item instance
        item_instance, _ = app.models.Item.objects.get_or_create(
            media_id=str(tmdb_id),
            source=Sources.TMDB.value,
            media_type=media_type,
            defaults={
                "title": metadata["title"],
                "image": metadata.get("image", settings.IMG_NONE),
            },
        )

        # Create media entry
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(
            item=item_instance,
            user=self.user,
            score=score,
            progress=(
                metadata.get("max_progress", 1)
                if status == Status.COMPLETED.value
                else 0
            ),
            status=status,
        )
        instance._history_date = timezone.now()
        self.bulk_media[media_type].append(instance)

    def _get_tmdb_id(self, imdb_id, media_type):
        """Get TMDB ID from IMDB ID."""
        try:
            # Use TMDB find endpoint to get TMDB ID from IMDB ID
            response = app.providers.tmdb.find(f"tt{imdb_id}", "imdb_id")

            if media_type == MediaTypes.MOVIE.value:
                results = response.get("movie_results", [])
            else:  # TV
                results = response.get("tv_results", [])

            if results:
                return results[0]["id"]

            return None
        except app.providers.services.ProviderAPIError as error:
            logger.warning("Error getting TMDB ID for IMDB ID %s: %s", imdb_id, error)
            return None

    def _get_metadata(self, media_type, tmdb_id, _title):
        """Get metadata from TMDB."""
        try:
            if media_type == MediaTypes.MOVIE.value:
                return app.providers.tmdb.movie(tmdb_id)
            # TV
            return app.providers.tmdb.tv(tmdb_id)
        except app.providers.services.ProviderAPIError as error:
            logger.warning(
                "Error getting metadata for %s %s: %s",
                media_type,
                tmdb_id,
                error,
            )
            return None
