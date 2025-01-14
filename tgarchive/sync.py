import datetime
import sys
from io import BytesIO
from sys import exit
import json
import logging
import os
import tempfile
import shutil
import time
from typing import List
from typing import Optional
from telethon import utils
from pathlib import Path

from PIL import Image
from telethon import TelegramClient, errors, sync
import telethon.tl.types

from .db import Migration
from .db import User, Message, Media


class Sync:
    """
    Sync iterates and receives messages from the Telegram group to the
    local SQLite DB.
    """
    config = {}
    db = None

    ignore_avatars = []

    def __init__(self, config, session_file, db):
        self.config = config
        self.db = db

        self.client = self.new_client(session_file, config)

        if not os.path.exists(self.config["media_dir"]):
            os.mkdir(self.config["media_dir"])

    def list(self):
        for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, telethon.tl.types.Chat):
                if entity.migrated_to:
                    print('m', entity.id, entity.title, f"({entity.migrated_to.channel_id})")
                    continue
                print('c', entity.id, entity.title)
            elif isinstance(entity, telethon.tl.types.User):
                if entity.deleted:
                    continue
                print('u', entity.id, entity.first_name, entity.last_name or '',
                      entity.username or '')
            elif isinstance(entity, telethon.tl.types.Channel):
                print('c', entity.id, entity.title)

    def sync(self, ids=None, from_id=None):
        """
        Sync syncs messages from Telegram from the last synced message
        into the local SQLite DB.
        """

        last_id = ids or from_id

        group_id = self._get_group_id(self.config["group"])

        if migrated_from_id := self._check_migration(group_id):
            self.sync_chat(migrated_from_id, last_id, ids, group_id)
        self.sync_chat(group_id, last_id, ids)

    def _check_migration(self, chat_id) -> Optional[int]:
        """
        check if chat is migrated, either returns migrated from chat_id or None
        """
        migration: Migration = self.db.get_migration(chat_id)
        if migration:
            return migration.from_chat_id if migration.done == 0 else None

        migrated_chat_id = self._get_migrated_from(chat_id)
        self.db.insert_migration(chat_id, migrated_chat_id,
                                 0 if migrated_chat_id else 1)
        return migrated_chat_id

    def _get_migrated_from(self, chat_id) -> Optional[int]:
        """
        Chat dialogs can be converted to channels, they are basically the
        same thing but with different ids.
        """
        for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, telethon.tl.types.Chat) and entity.migrated_to:
                if entity.migrated_to.channel_id == chat_id:
                    return entity.id
        return None

    def sync_chat(self, chat_id: int, last_id: int, ids, migrated_chat_id: Optional[int] = None):

        logging.info(f"starting syncing chat: {chat_id}, "
                     f"last_id: {last_id}, "
                     f"ids: {ids}, "
                     f"migrated_chat_id: {migrated_chat_id}")

        n = 0
        last_date = None

        if not last_id:
            last_id, last_date = self.db.get_last_message_id(chat_id)
            logging.info("fetching from last message id={} ({})".format(
                last_id, last_date))

        while True:
            has = False

            messages = self._get_messages(
                chat_id,
                offset_id=last_id if last_id else 0,
                ids=ids)

            for m in messages:
                if not m:
                    continue

                has = True

                # Insert the records into DB.
                self.db.insert_user(m.user)

                if m.media:
                    self.db.insert_media(m.media)

                self.db.insert_message(m)

                last_date = m.date
                n += 1
                if n % 300 == 0:
                    logging.info("fetched {} messages".format(n))
                    self.db.commit()

                if 0 < self.config["fetch_limit"] <= n or ids:
                    has = False
                    break

            self.db.commit()
            if has:
                last_id = m.message_id
                logging.info("fetched {} messages. sleeping for {} seconds".format(
                    n, self.config["fetch_wait"]))
                time.sleep(self.config["fetch_wait"])
            else:
                if migrated_chat_id:
                    self.db.insert_migration(migrated_chat_id, chat_id, 1)
                break

        self.db.commit()
        if self.config.get("use_takeout", False):
            self.finish_takeout()
        logging.info(
            "finished. fetched {} messages. last message = {}".format(n, last_date or ''))

    def new_client(self, session, config):
        if "proxy" in config and config["proxy"].get("enable"):
            proxy = config["proxy"]
            client = TelegramClient(session, config["api_id"], config["api_hash"], proxy=(proxy["protocol"], proxy["addr"], proxy["port"]))
        else:
            client = TelegramClient(session, config["api_id"], config["api_hash"])
        # hide log messages
        # upstream issue https://github.com/LonamiWebs/Telethon/issues/3840
        client_logger = client._log["telethon.client.downloads"]
        client_logger._info = client_logger.info

        def patched_info(*args, **kwargs):
            if (
                args[0] == "File lives in another DC" or
                args[0] == "Starting direct file download in chunks of %d at %d, stride %d"
            ):
                return client_logger.debug(*args, **kwargs)
            client_logger._info(*args, **kwargs)
        client_logger.info = patched_info

        client.start()
        if config.get("use_takeout", False):
            for retry in range(3):
                try:
                    takeout_client = client.takeout(finalize=True).__enter__()
                    # check if the takeout session gets invalidated
                    takeout_client.get_messages("me")
                    return takeout_client
                except errors.TakeoutInitDelayError as e:
                    logging.info(
                        "please allow the data export request received from Telegram on your device. "
                        "you can also wait for {} seconds.".format(e.seconds))
                    logging.info(
                        "press Enter key after allowing the data export request to continue..")
                    input()
                    logging.info("trying again.. ({})".format(retry + 2))
                except errors.TakeoutInvalidError:
                    logging.info(
                        "takeout invalidated. delete the session.session file and try again.")
                    raise
            else:
                logging.info("could not initiate takeout.")
                raise(Exception("could not initiate takeout."))
        else:
            return client

    def finish_takeout(self):
        self.client.__exit__(None, None, None)

    def _get_messages(self, group, offset_id, ids=None) -> Optional[List[Message]]:
        messages = self._fetch_messages(group, offset_id, ids)

        if len(messages) == 0:
            return None

        # https://docs.telethon.dev/en/latest/quick-references/objects-reference.html#message
        for m in messages:
            if not m or not m.sender:
                continue

            # Media.
            sticker = None
            med = None
            if m.media:
                # If it's a sticker, get the alt value (unicode emoji).
                if isinstance(m.media, telethon.tl.types.MessageMediaDocument) and \
                        hasattr(m.media, "document") and \
                        m.media.document.mime_type == "application/x-tgsticker":
                    alt = [a.alt for a in m.media.document.attributes if isinstance(
                        a, telethon.tl.types.DocumentAttributeSticker)]
                    if len(alt) > 0:
                        sticker = alt[0]
                elif isinstance(m.media, telethon.tl.types.MessageMediaPoll):
                    med = self._make_poll(m)
                else:
                    med = self._get_media(m)

            # Message.
            typ = "message"
            if m.action:
                if isinstance(m.action, telethon.tl.types.MessageActionChatAddUser):
                    typ = "user_joined"
                elif isinstance(m.action, telethon.tl.types.MessageActionChatDeleteUser):
                    typ = "user_left"

            yield Message(
                type=typ,
                message_id=m.id,
                chat_id=group,
                date=m.date,
                edit_date=m.edit_date,
                content=sticker if sticker else m.raw_text,
                reply_to=m.reply_to_msg_id if m.reply_to and m.reply_to.reply_to_msg_id else None,
                user=self._get_user(m.sender),
                media=med
            )

    def _fetch_messages(self, group, offset_id, ids=None) -> Message:
        try:
            if self.config.get("use_takeout", False):
                wait_time = 0
            else:
                wait_time = None
            messages = self.client.get_messages(group, offset_id=offset_id,
                                                limit=self.config["fetch_batch_size"],
                                                wait_time=wait_time,
                                                ids=ids,
                                                reverse=True)
            return messages
        except errors.FloodWaitError as e:
            logging.info(
                "flood waited: have to wait {} seconds".format(e.seconds))

    def _get_user(self, u) -> User:
        tags = []
        is_normal_user = isinstance(u, telethon.tl.types.User)

        if isinstance(u, telethon.tl.types.ChannelForbidden):
            return User(
                id=u.id,
                username=u.title,
                first_name=None,
                last_name=None,
                tags=tags,
                avatar=None
            )

        if is_normal_user:
            if u.bot:
                tags.append("bot")

        if u.scam:
            tags.append("scam")

        if u.fake:
            tags.append("fake")

        # Download sender's profile photo if it's not already cached.
        avatar = None
        if self.config["download_avatars"]:
            try:
                fname = self._download_avatar(u)
                avatar = fname
            except Exception as e:
                logging.error(
                    "error downloading avatar: #{}: {}".format(u.id, e))

        return User(
            id=u.id,
            username=u.username if u.username else str(u.id),
            first_name=u.first_name if is_normal_user else None,
            last_name=u.last_name if is_normal_user else None,
            tags=tags,
            avatar=avatar
        )

    def _make_poll(self, msg):
        if not msg.media.results or not msg.media.results.results:
            return None

        options = [{"label": a.text, "count": 0, "correct": False}
                   for a in msg.media.poll.answers]

        total = msg.media.results.total_voters
        if msg.media.results.results:
            for i, r in enumerate(msg.media.results.results):
                options[i]["count"] = r.voters
                options[i]["percent"] = r.voters / \
                    total * 100 if total > 0 else 0
                options[i]["correct"] = r.correct

        return Media(
            id=msg.id,
            type="poll",
            url=None,
            title=msg.media.poll.question,
            description=json.dumps(options),
            thumb=None
        )

    @staticmethod
    def get_media_type(media: telethon.tl.types.TypeMessageMedia) -> str:
        if isinstance(media, telethon.tl.types.MessageMediaPhoto):
            return "photo"

        if utils.is_video(media):
            return "video"

        if utils.is_audio(media):
            return "audio"

        return "document"


    def _get_media(self, msg):
        if isinstance(msg.media, telethon.tl.types.MessageMediaWebPage) and \
                not isinstance(msg.media.webpage, telethon.tl.types.WebPageEmpty):
            return Media(
                id=msg.id + utils.get_peer_id(msg.peer_id),
                type="webpage",
                url=msg.media.webpage.url,
                title=msg.media.webpage.title,
                description=msg.media.webpage.description if msg.media.webpage.description else None,
                thumb=None
            )
        elif isinstance(msg.media, telethon.tl.types.MessageMediaPhoto) or \
                isinstance(msg.media, telethon.tl.types.MessageMediaDocument) or \
                isinstance(msg.media, telethon.tl.types.MessageMediaContact):
            if self.config["download_media"]:
                # Filter by extensions?
                if len(self.config["media_mime_types"]) > 0:
                    if hasattr(msg, "file") and hasattr(msg.file, "mime_type") and msg.file.mime_type:
                        if msg.file.mime_type not in self.config["media_mime_types"]:
                            logging.info(
                                "skipping media #{} / {}".format(msg.file.name, msg.file.mime_type))
                            return

                logging.info("downloading media #{}".format(msg.id))
                try:
                    basename, fname, thumb = self._download_media(msg)
                    return Media(
                        id=msg.id + utils.get_peer_id(msg.peer_id),
                        type=self.get_media_type(msg.media),
                        url=fname,
                        title=basename,
                        description=None,
                        thumb=thumb
                    )
                except Exception as e:
                    logging.error(
                        "error downloading media: #{}: {}".format(msg.id, e))

    def _download_media(self, msg) -> [str, str, str]:
        """
        Download a media / file attached to a message and return its original
        filename, sanitized name on disk, and the thumbnail (if any). 
        """
        # Download the media to the temp dir and copy it back as
        # there does not seem to be a way to get the canonical
        # filename before the download.

        def _date_fmt(date: datetime.datetime) -> str:
            return date.strftime("%Y%m%d-%H%M%S")

        def _date_folders(date: datetime.datetime) -> str:
            return date.strftime("%Y%m")

        def _get_filename(msg, prefix, suffix) -> str:
            return f"{prefix}{_date_fmt(msg.date)}-{msg.id}{suffix}"

        def _get_download_dir(msg):
            path = Path(f"{self.config['media_dir']}/"\
                        f"{_date_folders(msg.date)}/").absolute()
            path.mkdir(exist_ok=True)
            return path

        download_dir = _get_download_dir(msg)

        fpath = self.client.download_media(msg, file=tempfile.gettempdir())

        if not isinstance(fpath, str):
            raise Exception()

        basename = Path(fpath).name
        suffix = Path(fpath).suffix

        newname = _get_filename(msg, "", suffix)

        shutil.move(fpath, download_dir.joinpath(newname))

        # If it's a photo, download the thumbnail.
        tname = None
        if isinstance(msg.media, telethon.tl.types.MessageMediaPhoto):
            tpath = self.client.download_media(msg,
                                               file=tempfile.gettempdir(),
                                               thumb=1)
            if isinstance(tpath, str):
                shutil.move(tpath, download_dir.joinpath(
                    _get_filename(msg, "thumb_", suffix)))

        return basename, newname, tname

    def _get_file_ext(self, f) -> str:
        if "." in f:
            e = f.split(".")[-1]
            if len(e) < 6:
                return e

        return ".file"

    def _download_avatar(self, user):
        fname = "avatar_{}.jpg".format(user.id)
        fpath = os.path.join(self.config["media_dir"], fname)

        if os.path.exists(fpath):
            return fname

        if user.id in self.ignore_avatars:
            return None

        logging.info("downloading avatar #{}".format(user.id))

        # Download the file into a container, resize it, and then write to disk.
        b = BytesIO()
        profile_photo = self.client.download_profile_photo(user, file=b)
        if profile_photo is None:
            logging.info("user has no avatar #{}".format(user.id))
            self.ignore_avatars.append(user.id)
            return None

        im = Image.open(b)
        im.thumbnail(self.config["avatar_size"], Image.ANTIALIAS)
        im.save(fpath, "JPEG")

        return fname

    def _get_group_id(self, group):
        """
        Syncs the Entity cache and returns the Entity ID for the specified group,
        which can be a str/int for group ID, group name, or a group username.

        The authorized user must be a part of the group.
        """
        # Get all dialogs for the authorized user, which also
        # syncs the entity cache to get latest entities
        # ref: https://docs.telethon.dev/en/latest/concepts/entities.html#getting-entities
        _ = self.client.get_dialogs()

        try:
            # If the passed group is a group ID, extract it.
            group = int(group)
        except ValueError:
            # Not a group ID, we have either a group name or
            # a group username: @group-username
            pass

        try:
            entity = self.client.get_entity(group)
        except ValueError:
            logging.critical("the group: {} does not exist,"
                             " or the authorized user is not a participant!".format(group))
            # This is a critical error, so exit with code: 1
            exit(1)

        return entity.id
