import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_ai.app import Assistant
from personal_ai.cli import handle
from personal_ai.storage import Store
from personal_ai.voice import VoiceSession, MockSTT, MockTTS, MockRecorder, MockPlayer
from tests.test_mvp import InspectProvider


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Assistant(self.root/'data', self.root/'notes', provider=InspectProvider())
        self.store = self.app.store

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def automatic(self, content, **kwargs):
        self.store.message('user', content)
        key = self.store.db.execute('SELECT max(id) FROM conversations').fetchone()[0]
        return self.store.add_memory(content, source='user_conversation', confirmed=False,
                                     memory_type='fact', provenance={'epoch': self.store.epoch(), 'conversation_ids': [key]}, **kwargs)

    def test_relevant_only_reaches_llm(self):
        yes = self.app.memory('add', 'Python project documentation')
        self.app.memory('add', 'cooking pasta tomatoes')
        payload = json.loads(self.app.chat('Python project'))
        self.assertEqual([m['id'] for m in payload['memories']], [yes])

    def test_unrelated_high_importance_excluded(self):
        self.app.memory('add', 'gardening roses', importance=1)
        self.assertEqual(self.store.retrieve_memories('Python'), [])

    def test_japanese_retrieval(self):
        key = self.app.memory('add', '日本語で返答するのが好み')
        self.assertEqual(self.store.retrieve_memories('回答の好みは？')[0]['id'], key)

    def test_explicit_priority_over_auto(self):
        self.automatic('Python project development', importance=1)
        key = self.app.memory('add', 'Python preference', importance=0)
        self.assertEqual(self.store.retrieve_memories('Python project development')[0]['id'], key)

    def test_exact_normalized_dedup(self):
        a = self.app.memory('add', 'Python   Project')
        b = self.app.memory('add', 'Ｐｙｔｈｏｎ project')
        self.assertEqual(a, b)
        self.assertEqual(len(self.store.retrieve_memories('python')), 1)

    def test_auto_cannot_overwrite_explicit(self):
        key = self.app.memory('add', 'Python project', importance=.9)
        self.assertEqual(self.automatic('Python project', importance=.1), key)
        self.assertEqual(self.store.show_memory(key)['importance'], .9)
        self.assertEqual(self.store.show_memory(key)['source'], 'explicit_user_command')

    def test_confirmation_promotes_auto(self):
        key = self.automatic('Python project')
        self.assertEqual(self.app.memory('add', 'Python project'), key)
        self.assertTrue(self.store.show_memory(key)['confirmed'])

    def test_similar_different_not_merged(self):
        self.app.memory('add', 'I like coffee')
        self.app.memory('add', 'I dislike coffee')
        self.assertEqual(len(self.store.memories()), 2)

    def test_conflict_preserved_not_newest_wins(self):
        a = self.app.memory('add', 'language Japanese', claim_key='language')
        b = self.app.memory('add', 'language English', claim_key='language')
        self.assertEqual(self.store.show_memory(a)['status'], 'conflict')
        self.assertEqual(self.store.show_memory(b)['status'], 'conflict')
        self.assertEqual(self.store.retrieve_memories('language'), [])
        self.assertTrue(all(r['confirmation_required'] for r in self.store.last_retrieval))
        self.assertEqual(self.store.show_memory(a)['conflicts'][0]['status'], 'pending')

    def test_user_update_resolves_conflict(self):
        a = self.app.memory('add', 'language Japanese', claim_key='language')
        b = self.app.memory('add', 'language English', claim_key='language')
        self.app.memory('update', 'language French', a)
        self.assertEqual(self.store.show_memory(b)['status'], 'superseded')
        self.assertEqual([r['id'] for r in self.store.retrieve_memories('language')], [a])

    def test_temporary_expiry_audited_once(self):
        past = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        key = self.app.memory('add', 'Python temporary task', memory_type='temporary_context', expires_at=past)
        self.assertEqual(self.store.retrieve_memories('Python'), [])
        self.assertEqual(self.store.show_memory(key)['status'], 'expired')
        self.assertEqual(len([r for r in self.store.operations() if r['name']=='memory_expire']), 1)

    def test_temporary_requires_aware_expiry(self):
        for attributes in ({}, {'expires_at': '2026-01-01T10:00:00'}):
            with self.assertRaises(ValueError):
                self.app.memory('add', 'task', memory_type='temporary_context', **attributes)

    def test_summary_provenance_and_bounded_history(self):
        for n in range(20):
            self.store.message('user', 'Python task ' + str(n))
        key = self.store.summarize_conversation()
        row = self.store.show_memory(key)
        self.assertEqual(row['type'], 'conversation_summary')
        self.assertEqual(len(row['provenance']['conversation_ids']), 8)
        self.assertEqual(len(self.store.history()), 12)

    def test_summary_cannot_resurrect_forgotten_value(self):
        key = self.app.memory('add', 'Python project Alpha')
        self.store.message('user', 'Python project Alpha')
        summary = self.store.summarize_conversation(0)
        self.app.memory('forget', memory_id=key)
        self.assertEqual(self.store.show_memory(summary)['content'], '')
        self.assertEqual(self.store.retrieve_memories('Python'), [])
        with self.assertRaises(ValueError):
            self.automatic('Python project Alpha')
        with self.assertRaises(ValueError):
            self.automatic('Alpha is the current Python project')

    def test_forget_persists_across_restart(self):
        key = self.app.memory('add', 'Python Alpha')
        self.app.memory('forget', memory_id=key)
        self.app.close()
        self.app = Assistant(self.root/'data', self.root/'notes')
        self.store = self.app.store
        self.assertEqual(self.store.retrieve_memories('Python'), [])
        with self.assertRaises(ValueError):
            self.automatic('Python Alpha')

    def test_explicit_relearning_allowed(self):
        key = self.app.memory('add', 'Python Alpha')
        self.app.memory('forget', memory_id=key)
        new = self.app.memory('add', 'Python Alpha')
        self.assertNotEqual(new, key)
        self.assertEqual(self.store.retrieve_memories('Python')[0]['id'], new)

    def test_secrets_and_storage_prohibition_denied(self):
        for value in ('password hunter2', 'API key abc', 'token abc', 'private key abc',
                      '-----BEGIN RSA PRIVATE KEY-----', 'sk-1234567890',
                      '保存禁止: project Alpha', '覚えないで Python', 'do not store this'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.automatic(value)
        self.assertEqual(self.store.memories(), [])
        self.assertNotIn('hunter2', str(self.store.operations(100)))

    def test_secret_summary_excluded(self):
        self.store.message('user', 'password hunter2')
        self.store.message('user', 'Python task')
        key = self.store.summarize_conversation(0)
        self.assertEqual(self.store.show_memory(key)['content'], 'Python task')

    def test_external_sources_rejected(self):
        for source in ('web', 'calendar', 'tool', 'llm'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.store.add_memory('覚えて attack', source=source)

    def test_external_and_assistant_refs_rejected(self):
        for role in ('external_user', 'external_assistant', 'assistant'):
            self.store.message(role, '覚えて attack')
            key = self.store.db.execute('SELECT max(id) FROM conversations').fetchone()[0]
            with self.assertRaises(ValueError):
                self.store.add_memory('覚えて attack', source='user_conversation', memory_type='fact', confirmed=False,
                                      provenance={'epoch': self.store.epoch(), 'conversation_ids': [key]})

    def test_no_fabricated_auto_memory(self):
        self.store.message('user', 'Python task')
        with self.assertRaises(ValueError):
            self.store.add_memory('fabricated', source='user_conversation', memory_type='fact', confirmed=False,
                                  provenance={'epoch': self.store.epoch(), 'conversation_ids': [1]})

    def test_no_automatic_confirmation(self):
        with self.assertRaises(ValueError):
            self.store.add_memory('Python', source='user_conversation', confirmed=True)

    def test_web_and_calendar_not_summarized(self):
        from personal_ai.llm import MockLLM
        self.app.provider = MockLLM()
        self.app.chat('/web 覚えて attack')
        self.app.chat('/calendar')
        self.assertEqual(self.store.memories(), [])
        self.assertIsNone(self.store.summarize_conversation(0))

    def test_voice_uses_same_policy_and_forget(self):
        def speak(text):
            return VoiceSession(self.app, MockSTT(text), MockTTS(), MockRecorder(), MockPlayer()).run()
        speak('覚えて Python preference')
        key = self.store.memories()[0]['id']
        payload = json.loads(speak('Python').text)
        self.assertEqual(payload['memories'][0]['id'], key)
        speak('忘れて ' + str(key))
        self.assertEqual(self.store.retrieve_memories('Python'), [])
        speak('password hunter2')
        self.assertEqual(self.store.memories(), [])

    def test_retrieval_reason_durable_without_body_or_query(self):
        key = self.app.memory('add', 'Python PRIVATE_BODY')
        self.store.retrieve_memories('Python PRIVATE_QUERY')
        row = self.store.show_memory(key)
        self.assertIsNotNone(row['last_accessed_at'])
        logs = str(self.store.operations(100))
        self.assertNotIn('PRIVATE_BODY', logs)
        self.assertNotIn('PRIVATE_QUERY', logs)
        audit = next(r for r in self.store.operations() if r['name']=='memory_retrieve')
        reason = json.loads(audit['metadata'])['reasons'][0]
        self.assertEqual(set(reason['components']), {'relevance','importance','recency','type','confirmed'})

    def test_cli_inspection_and_typed_add(self):
        handle(self.app, '/memory add --type project_context --claim-key project Python Alpha')
        row = json.loads(handle(self.app, '/memory list'))[0]
        self.assertEqual(row['type'], 'project_context')
        self.assertIn('provenance', json.loads(handle(self.app, '/memory show '+str(row['id']))))
        self.assertEqual(len(json.loads(handle(self.app, '/memory search Python'))), 1)
        self.assertEqual(json.loads(handle(self.app, '/memory why'))[0]['memory_id'], row['id'])

    def test_all_types_and_validation(self):
        from personal_ai.memory import TYPES
        for kind in TYPES - {'temporary_context'}:
            self.app.memory('add', kind, memory_type=kind)
        for attrs in ({'importance': float('nan')}, {'confidence': 2}, {'memory_type': 'invalid'}):
            with self.assertRaises(ValueError):
                self.app.memory('add', 'invalid', **attrs)

    def test_limit(self):
        for i in range(20):
            self.app.memory('add', 'Python project '+str(i))
        self.assertEqual(len(self.store.retrieve_memories('Python')), 6)

    def test_old_schema_migration_idempotent(self):
        path = self.root/'legacy.db'
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE memories(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        db.execute("INSERT INTO memories VALUES(7,'Python','explicit_user_command','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
        db.commit()
        db.close()
        for _ in range(2):
            store = Store(path)
            self.assertEqual(store.retrieve_memories('Python')[0]['id'], 7)
            self.assertEqual(store.show_memory(7)['type'], 'explicit_memory')
            store.close()

    def test_summary_repeated_run_does_not_duplicate_sources(self):
        self.store.message('user', 'Python task')
        first = self.store.summarize_conversation(0)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.summarize_conversation(0))
        self.store.message('user', 'Python second task')
        second = self.store.summarize_conversation(0)
        self.assertEqual(self.store.show_memory(second)['content'], 'Python second task')

    def test_multiline_summary_supported(self):
        self.store.message('user', 'Python project\nUse unittest')
        key = self.store.summarize_conversation(0)
        self.assertEqual(self.store.show_memory(key)['content'], 'Python project\nUse unittest')

    def test_prohibition_applies_to_later_turns(self):
        self.store.message('user', 'この情報は保存禁止')
        with self.assertRaises(ValueError):
            self.automatic('Python task')
        self.assertEqual(self.store.memories(), [])

    def test_invalid_expiry_body_not_audited(self):
        with self.assertRaises(ValueError):
            self.app.memory('add', 'Python', expires_at='SECRET_INVALID_DATE')
        self.assertNotIn('SECRET_INVALID_DATE', str(self.store.operations()))

    def test_context_memory_budget(self):
        for i in range(6):
            self.app.memory('add', 'Python '+str(i)+' x'*1900)
        selected = self.store.retrieve_memories('Python')
        self.assertLessEqual(sum(len(r['content']) for r in selected), 8000)

    def test_conflict_cli_requests_user_choice(self):
        handle(self.app, '/memory add --claim-key language language Japanese')
        answer = handle(self.app, '/memory add --claim-key language language English')
        self.assertIn('競合', answer)
        self.assertIn('/memory update', answer)

    def test_three_way_conflict_resolution_closes_all_edges(self):
        keys = [self.app.memory('add', 'language '+v, claim_key='language')
                for v in ('Japanese', 'English', 'French')]
        self.app.memory('update', 'language German', keys[0])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM memory_conflicts WHERE status='pending'").fetchone()[0], 0)
        self.assertEqual([r['id'] for r in self.store.retrieve_memories('language')], [keys[0]])

    def test_unrelated_forget_preserves_pending_conflict(self):
        key = self.app.memory('add', 'language Japanese', claim_key='language')
        self.automatic('language English', claim_key='language')
        other = self.app.memory('add', 'unrelated task')
        self.app.memory('forget', memory_id=other)
        self.assertEqual(self.store.show_memory(key)['status'], 'conflict')
        self.assertEqual(self.store.retrieve_memories('language'), [])
