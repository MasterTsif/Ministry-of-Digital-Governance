"""Exercise notification buttons and all alert types without contacting Discord."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
from discord.webhook.async_ import async_context

from ministry_bot.core.bot import MinistryBot
from ministry_bot.exams.discord_cog import Exams
from ministry_bot.exams.exam_monitor import RoomChange
from ministry_bot.notifications.discord_cog import Notifications
from ministry_bot.notifications.subscriptions import SUBSCRIPTION_PROMPT, SubscriptionMessage
from ministry_bot.schedule.discord_cog import Schedule
from ministry_bot.schedule.pdf_storage import PdfSnapshot
from tests.helpers import exam_snapshot, isolated_settings, mock_alert_channel


class NotificationButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with isolated_settings(
            Path(self.directory.name) / "bot.sqlite3", NOTIFICATIONS_ENABLED="true",
        ) as settings:
            self.settings = settings
        self.role = MagicMock(spec=discord.Role)
        self.role.id = self.settings.role_id
        self.role.mention = f"<@&{self.role.id}>"
        self.role.is_default.return_value = False
        self.role.managed = False
        self.role.mentionable = True
        self.role.permissions = discord.Permissions.none()
        self.role.__ge__.return_value = False
        self.member = SimpleNamespace(id=42, roles=[], pending=False, bot=False)

        async def add_roles(role, **kwargs):
            await asyncio.sleep(0)
            self.member.roles.append(role)

        async def remove_roles(role, **kwargs):
            await asyncio.sleep(0)
            self.member.roles = [item for item in self.member.roles if item.id != role.id]

        self.member.add_roles = AsyncMock(side_effect=add_roles)
        self.member.remove_roles = AsyncMock(side_effect=remove_roles)
        self.guild = SimpleNamespace(
            id=self.settings.guild_id,
            get_role=lambda _: self.role,
            me=SimpleNamespace(
                guild_permissions=discord.Permissions(manage_roles=True), top_role=self.role,
            ),
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.bot_user = SimpleNamespace(id=999)
        self.channel = mock_alert_channel(self.settings)
        self.channel.guild = self.guild
        self.channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
        )
        self.messages = {}
        message_ids = count(500)

        async def send(*args, **kwargs):
            message = SimpleNamespace(
                id=next(message_ids), guild=self.guild, channel=self.channel,
                author=self.bot_user, embeds=[], components=[],
            )
            message.jump_url = f"https://discord.com/channels/{self.guild.id}/{message.channel.id}/{message.id}"

            def update(**payload):
                if "embed" in payload:
                    message.embeds = [payload["embed"]]
                if payload.get("view") is not None:
                    message.components = [
                        discord.ActionRow(row) for row in payload["view"].to_components()
                    ]
                return message

            message.edit = AsyncMock(side_effect=update)
            update(**kwargs)
            self.messages[message.id] = message
            return message

        self.channel.send = AsyncMock(side_effect=send)
        self.channel.fetch_message = AsyncMock(side_effect=lambda message_id: self.messages[message_id])
        self.bot = SimpleNamespace(
            settings=self.settings, user=self.bot_user, http_session=None,
            get_guild=lambda _: self.guild, get_channel=lambda _: self.channel, add_view=Mock(),
        )
        self.cog = Notifications(self.bot)
        self.bot.get_cog = lambda name: self.cog if name == "Notifications" else None

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def request(self, message):
        response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: True)
        return SimpleNamespace(
            guild_id=self.guild.id, channel_id=message.channel.id, message=message,
            user=SimpleNamespace(id=self.member.id), response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def feedback(self, request):
        payload = request.followup.send.await_args
        self.assertTrue(payload.kwargs["ephemeral"])
        self.assertEqual(payload.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        request.response.send_message.assert_not_awaited()
        return payload.args[0]

    async def click(self, request, *, subscribe):
        button = self.cog.persistent_view.children[0 if subscribe else 1]
        await button.callback(request)

    async def test_startup_creates_one_panel_with_explanations_and_persistent_buttons(self):
        await self.cog.cog_load()
        self.bot.add_view.assert_called_once_with(self.cog.persistent_view)
        await self.cog.on_ready()
        await self.cog.on_ready()
        message = next(iter(self.messages.values()))
        self.channel.send.assert_awaited_once()
        message.edit.assert_not_awaited()
        self.assertEqual(message.embeds[0].description, SUBSCRIPTION_PROMPT)
        view = self.channel.send.await_args.kwargs["view"]
        self.assertTrue(view.is_persistent())
        self.assertIsNone(view.timeout)
        for item in view.children:
            self.assertTrue(item.label)
            self.assertRegex(item.label, r"[\u0370-\u03ff]")
        self.assertEqual(view.children[0].style, discord.ButtonStyle.success)
        self.assertIn(view.children[1].style, [
            discord.ButtonStyle.secondary, discord.ButtonStyle.danger,
        ])
        self.assertEqual(len({item.custom_id for item in view.children}), 2)
        self.assertTrue(self.cog.has_subscription_buttons(message))
        self.assertTrue(self.cog.subscription_store.initial(self.guild.id).ready)
        self.assertFalse(self.channel.permissions_for.return_value.add_reactions)
        self.assertFalse(self.channel.permissions_for.return_value.manage_messages)

    async def test_both_buttons_confirm_the_completed_role_change_privately(self):
        message = await self.cog.ensure_initial_subscription_message()
        subscribe = self.request(message)

        async def add_after_acknowledgement(role, **kwargs):
            subscribe.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            subscribe.followup.send.assert_not_awaited()
            self.member.roles.append(role)

        self.member.add_roles.side_effect = add_after_acknowledgement
        await self.click(subscribe, subscribe=True)
        self.member.add_roles.assert_awaited_once_with(
            self.role, reason="Εγγραφή στις ειδοποιήσεις προγράμματος", atomic=True,
        )
        self.assertIn("Σου δόθηκε ο ρόλος", self.feedback(subscribe))
        self.assertIn(self.role.mention, self.feedback(subscribe))

        unsubscribe = self.request(message)
        await self.click(unsubscribe, subscribe=False)
        self.member.remove_roles.assert_awaited_once_with(
            self.role, reason="Διαγραφή από τις ειδοποιήσεις προγράμματος", atomic=True,
        )
        self.assertIn("αφαιρέθηκε", self.feedback(unsubscribe))
        self.assertEqual(self.member.roles, [])
        message.edit.assert_not_awaited()

    async def test_repeated_clicks_explain_the_existing_state_without_changing_roles(self):
        message = await self.cog.ensure_initial_subscription_message()
        self.member.roles = [self.role]
        subscribed = self.request(message)
        await self.click(subscribed, subscribe=True)
        self.member.add_roles.assert_not_awaited()
        self.assertIn("ήδη ενεργές", self.feedback(subscribed))
        self.member.roles = []
        unsubscribed = self.request(message)
        await self.click(unsubscribed, subscribe=False)
        self.member.remove_roles.assert_not_awaited()
        self.assertIn("ήδη ανενεργές", self.feedback(unsubscribed))

    async def test_concurrent_subscribe_clicks_add_the_role_once(self):
        message = await self.cog.ensure_initial_subscription_message()
        first, second = self.request(message), self.request(message)
        await asyncio.gather(self.click(first, subscribe=True), self.click(second, subscribe=True))
        self.member.add_roles.assert_awaited_once()
        results = [self.feedback(first), self.feedback(second)]
        self.assertEqual(sum("Σου δόθηκε" in result for result in results), 1)
        self.assertEqual(sum("ήδη ενεργές" in result for result in results), 1)

    async def test_role_permission_failure_has_no_success_confirmation(self):
        message = await self.cog.ensure_initial_subscription_message()
        self.member.add_roles.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions",
        )
        request = self.request(message)
        with self.assertLogs("ministry_bot.notifications.discord_cog", level="ERROR"):
            await self.click(request, subscribe=True)
        result = self.feedback(request)
        self.assertIn("❌", result)
        self.assertNotIn("Σου δόθηκε", result)
        self.assertNotIn("Missing Permissions", result)
        self.assertEqual(self.member.roles, [])
        self.assertEqual(self.cog.last_error, "Discord denied the notification role change.")

    async def test_unrecognized_message_cannot_assign_a_role(self):
        message = await self.cog.ensure_initial_subscription_message()
        message.id += 100
        request = self.request(message)
        with self.assertLogs("ministry_bot.notifications.discord_cog", level="WARNING"):
            await self.click(request, subscribe=True)
        self.member.add_roles.assert_not_awaited()
        self.guild.fetch_member.assert_not_awaited()
        self.assertIn("δεν είναι διαθέσιμο", self.feedback(request))

    async def test_wrong_server_or_channel_cannot_assign_a_role(self):
        message = await self.cog.ensure_initial_subscription_message()
        for attribute in ("guild_id", "channel_id"):
            with self.subTest(attribute=attribute):
                request = self.request(message)
                setattr(request, attribute, 123456)
                with self.assertLogs("ministry_bot.notifications.discord_cog", level="WARNING"):
                    await self.click(request, subscribe=True)
                self.assertIn("❌", self.feedback(request))
        self.member.add_roles.assert_not_awaited()

    async def test_member_must_complete_server_screening_before_receiving_the_role(self):
        message = await self.cog.ensure_initial_subscription_message()
        self.member.pending = True
        request = self.request(message)
        with self.assertLogs("ministry_bot.notifications.discord_cog", level="WARNING"):
            await self.click(request, subscribe=True)
        self.member.add_roles.assert_not_awaited()
        self.assertIn("αποδοχή των κανόνων", self.feedback(request))

    async def test_all_schedule_and_exam_alerts_include_buttons_and_explanations_on_send(self):
        schedule = Schedule(self.bot)
        snapshot = PdfSnapshot(
            "https://example.test/schedule.pdf", "Class timetable", "a" * 64,
            None, None, datetime.now(UTC).isoformat(), None,
        )
        schedule.state_store.save_snapshot(snapshot, notify=True)
        await schedule.send_pending_notifications()
        self.assertEqual(schedule.state_store.get_pending_notifications(), [])

        exams = Exams(self.bot)
        before, after = exam_snapshot(room="Room 101"), exam_snapshot(room="Room 202")
        exams.state_store.save_snapshot(before, kind="new_period")
        await exams.send_pending_notifications()
        exams.state_store.save_snapshot(after, kind="rooms", changes=(RoomChange(before.events[0], after.events[0]),))
        await exams.send_pending_notifications()
        self.assertEqual(exams.state_store.get_pending_notifications(), [])
        self.assertEqual(self.channel.send.await_count, 3)
        for call in self.channel.send.await_args_list:
            self.assertTrue(call.kwargs["view"].is_persistent())
            self.assertEqual(len(call.kwargs["view"].children), 2)
            prompt = call.kwargs["embed"].fields[-1]
            self.assertTrue(prompt.name)
            self.assertEqual(prompt.value, SUBSCRIPTION_PROMPT)
            self.assertLessEqual(len(call.kwargs["embed"]), 6000)
        for message in self.messages.values():
            self.assertTrue(self.cog.subscription_store.get(message.id).ready)
            message.edit.assert_not_awaited()

    async def test_exam_buttons_work_in_a_separate_channel_using_the_same_role(self):
        self.settings = replace(self.settings, exams=replace(self.settings.exams, channel_id=201))
        self.bot.settings = self.settings
        self.cog = Notifications(self.bot)
        self.channel.id = 201
        exams = Exams(self.bot)
        exams.state_store.save_snapshot(exam_snapshot(), kind="new_period")
        await exams.send_pending_notifications()
        message = next(iter(self.messages.values()))
        request = self.request(message)
        await self.click(request, subscribe=True)
        self.assertIn("Σου δόθηκε", self.feedback(request))

    async def test_restart_registers_callbacks_and_uses_stored_alert_without_fetching_history(self):
        alert_id = 123456
        self.cog.subscription_store.remember(SubscriptionMessage(
            alert_id, self.guild.id, self.channel.id, self.role.id, ready=True,
        ))
        message = SimpleNamespace(id=alert_id, channel=self.channel, author=self.bot_user)
        async with MinistryBot(self.settings) as restarted:
            with patch.object(restarted.tree, "sync", new=AsyncMock(return_value=[])):
                await restarted.setup_hook()
            cog = restarted.get_cog("Notifications")
            self.assertIn(cog.persistent_view, restarted.persistent_views)
            restarted._connection.user = self.bot_user
            with patch.object(restarted, "get_guild", return_value=self.guild):
                with patch.object(restarted, "get_channel", return_value=self.channel):
                    request = self.request(message)
                    await cog.persistent_view.children[0].callback(request)
                    self.assertIn("Σου δόθηκε", self.feedback(request))
            self.channel.fetch_message.assert_not_awaited()

    async def test_unloading_stops_all_registered_button_views(self):
        await self.cog.cog_load()
        view = self.cog.create_subscription_view()
        await self.cog.cog_unload()
        self.assertTrue(view.is_finished())
        self.assertTrue(self.cog.persistent_view.is_finished())

    def preview_request(self):
        message = SimpleNamespace(
            id=700, channel=self.channel, guild=self.guild, author=self.bot_user,
        )
        request = self.request(message)
        request.followup.send.return_value = message
        request.edit_original_response = AsyncMock(return_value=message)
        request.channel = self.channel
        return request

    async def test_schedule_preview_is_private_and_preserves_the_snapshot_and_queue(self):
        schedule = Schedule(self.bot)
        snapshot = PdfSnapshot(
            "https://example.test/schedule.pdf", "Class timetable", "a" * 64,
            None, None, datetime.now(UTC).isoformat(), None,
        )
        schedule.state_store.save_snapshot(snapshot, notify=True)
        pending = schedule.state_store.get_pending_notifications()
        request = self.preview_request()
        await schedule.preview.callback(schedule, request)
        payload = request.edit_original_response.await_args
        request.response.defer.assert_awaited_once_with(ephemeral=True)
        request.followup.send.assert_not_awaited()
        self.assertEqual(payload.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertIn("Δοκιμαστική προεπισκόπηση", payload.kwargs["content"])
        self.assertIn(f"<@&{self.role.id}>", payload.kwargs["content"])
        self.assertEqual(payload.kwargs["embed"].to_dict(), schedule.build_schedule_notification_embed(snapshot, self.channel.id).to_dict())
        self.assertEqual(len(payload.kwargs["view"].children), 2)
        self.assertTrue(self.cog.subscription_store.get(700).ready)
        self.assertEqual(schedule.state_store.get_current_snapshot(), snapshot)
        self.assertEqual(schedule.state_store.get_pending_notifications(), pending)
        self.channel.send.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()

    async def test_schedule_preview_can_read_a_pdf_without_creating_a_baseline(self):
        schedule = Schedule(self.bot)
        snapshot = PdfSnapshot(
            "https://example.test/schedule.pdf", "Class timetable", "b" * 64,
            None, None, datetime.now(UTC).isoformat(), None,
        )
        schedule.pdf_monitor.pdf_client.fetch_pdf_snapshot = AsyncMock(return_value=snapshot)
        request = self.preview_request()
        await schedule.preview.callback(schedule, request)
        schedule.pdf_monitor.pdf_client.fetch_pdf_snapshot.assert_awaited_once_with(None)
        self.assertEqual(request.edit_original_response.await_args.kwargs["embed"].url, snapshot.url)
        self.assertIsNone(schedule.state_store.get_current_snapshot())
        self.assertEqual(schedule.state_store.get_pending_notifications(), [])

    async def test_schedule_preview_source_error_returns_a_private_greek_error(self):
        schedule = Schedule(self.bot)
        schedule.pdf_monitor.pdf_client.fetch_pdf_snapshot = AsyncMock(side_effect=RuntimeError("PDF unavailable"))
        request = self.preview_request()
        with self.assertLogs("ministry_bot.schedule.discord_cog", level="ERROR"):
            await schedule.preview.callback(schedule, request)
        payload = request.followup.send.await_args
        self.assertTrue(payload.kwargs["ephemeral"])
        self.assertIn("❌", payload.args[0])
        self.assertNotIn("PDF unavailable", payload.args[0])
        self.assertIsNone(schedule.state_store.get_current_snapshot())

    async def test_schedule_preview_requires_the_configured_alert_channel(self):
        schedule = Schedule(self.bot)
        request = self.preview_request()
        request.channel_id = 777
        await schedule.preview.callback(schedule, request)
        request.response.defer.assert_not_awaited()
        request.followup.send.assert_not_awaited()
        payload = request.response.send_message.await_args
        self.assertTrue(payload.kwargs["ephemeral"])
        self.assertIn(f"<#{self.settings.channel_id}>", payload.args[0])

    async def test_preview_registers_buttons_with_the_real_sdk_guild_context(self):
        schedule = Schedule(self.bot)
        snapshot = PdfSnapshot(
            "https://example.test/schedule.pdf", "Class timetable", "c" * 64,
            None, None, datetime.now(UTC).isoformat(), None,
        )
        schedule.state_store.save_snapshot(snapshot, notify=False)
        payload = {
            "id": "701", "channel_id": str(self.channel.id), "type": 0,
            "content": "Preview", "flags": 64, "components": [],
            "author": {"id": "999", "username": "Test bot", "discriminator": "0", "avatar": None},
        }
        state = SimpleNamespace(
            store_user=lambda data, **kwargs: self.bot_user,
            _get_guild=lambda _: None,
            allowed_mentions=discord.AllowedMentions.none(),
            http=SimpleNamespace(proxy=None, proxy_auth=None),
            store_view=Mock(),
        )
        partial_channel = discord.PartialMessageable(state=state, id=self.channel.id)
        webhook_message = discord.WebhookMessage(state=state, channel=partial_channel, data=payload)
        self.assertIsNone(webhook_message.guild)
        request = self.preview_request()
        request._state = state
        request._session = None
        request.application_id = self.bot_user.id
        request.token = "offline-interaction-token"
        request.followup.send.return_value = webhook_message
        adapter = SimpleNamespace(edit_original_interaction_response=AsyncMock(return_value=payload))

        async def edit_original_response(**kwargs):
            token = async_context.set(adapter)
            try:
                return await discord.Interaction.edit_original_response(request, **kwargs)
            finally:
                async_context.reset(token)

        request.edit_original_response.side_effect = edit_original_response
        await schedule.preview.callback(schedule, request)
        request.followup.send.assert_not_awaited()
        saved = self.cog.subscription_store.get(701)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.guild_id, self.guild.id)
        self.assertEqual(saved.channel_id, self.channel.id)
        self.assertTrue(saved.ready)
        self.assertIsNone(self.cog.last_error)
        state.store_view.assert_called_once()
        view, message_id = state.store_view.call_args.args
        self.assertEqual(message_id, 701)
        original = discord.InteractionMessage(state=state, channel=self.channel, data=payload)
        self.assertTrue(original.flags.ephemeral)
        click = self.request(original)
        await view.children[0].callback(click)
        self.assertIn("Σου δόθηκε", self.feedback(click))
        self.assertEqual(schedule.state_store.get_current_snapshot(), snapshot)
        self.assertEqual(schedule.state_store.get_pending_notifications(), [])


if __name__ == "__main__":
    unittest.main()
