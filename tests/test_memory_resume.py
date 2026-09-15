"""Phase 4.1 regressions: scoped suppression and trusted source boundaries."""
import json
import unittest

from personal_ai.app import Assistant
from personal_ai.storage import Store
from personal_ai.voice import VoiceSession, MockSTT, MockTTS, MockRecorder, MockPlayer
from tests import test_memory


class MemoryResumeTests(unittest.TestCase):
    setUp = test_memory.MemoryTests.setUp
    tearDown = test_memory.MemoryTests.tearDown
    automatic = test_memory.MemoryTests.automatic

    def forget(self):
        key = self.app.memory('add', 'Python Alpha')
        self.app.memory('forget', memory_id=key)
        return key

    def test_unrelated_automatic_continues(self):
        self.forget()
        key = self.automatic('gardening roses')
        self.assertEqual(self.store.retrieve_memories('roses')[0]['id'], key)
        self.assertEqual(self.store.db.execute('SELECT automatic_disabled FROM memory_policy').fetchone()[0], 0)

    def test_unrelated_existing_memory_and_history_survive(self):
        key = self.automatic('gardening roses')
        before = self.store.show_memory(key)
        self.forget()
        self.assertEqual(self.store.show_memory(key), before)
        self.assertEqual(self.store.history(), [{'role': 'user', 'content': 'gardening roses'}])

    def test_mixed_summary_regenerated_from_safe_originals(self):
        key = self.app.memory('add', 'Python Alpha')
        self.store.message('user', 'Python Alpha')
        self.store.message('user', 'gardening roses')
        summary = self.store.summarize_conversation(0)
        self.app.memory('forget', memory_id=key)
        self.assertEqual(self.store.show_memory(summary)['status'], 'stale')
        self.assertEqual(self.store.show_memory(summary)['content'], '')
        fresh = self.store.summarize_conversation(0)
        self.assertEqual(self.store.show_memory(fresh)['content'], 'gardening roses')
        self.assertIsNone(self.store.summarize_conversation(0))

    def test_new_summary_continues_after_forget(self):
        self.forget()
        self.store.message('user', 'gardening roses')
        key = self.store.summarize_conversation(0)
        self.assertEqual(self.store.show_memory(key)['content'], 'gardening roses')

    def test_partial_normalized_and_reordered_replay_denied(self):
        self.forget()
        for text in ('ＰＹＴＨＯＮ   ALPHA', 'Alpha is the current project', 'Alpha', 'notes: Python Alpha\nnew topic'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.automatic(text)
        self.assertIsNone(self.store.summarize_conversation(0))
        self.assertEqual(self.store.history(), [])

    def test_update_blocks_old_value_and_exposes_canonical(self):
        key = self.automatic('language Japanese')
        self.store.message('user', 'gardening roses')
        summary = self.store.summarize_conversation(0)
        self.app.memory('update', 'language French', key)
        row = self.store.show_memory(key)
        self.assertEqual((row['type'], row['confirmed'], row['content']), ('explicit_memory', 1, 'language French'))
        self.assertEqual(self.store.retrieve_memories('French')[0]['id'], key)
        self.assertEqual(self.store.retrieve_memories('Japanese'), [])
        self.assertEqual(self.store.show_memory(summary)['status'], 'stale')
        with self.assertRaises(ValueError):
            self.automatic('Japanese')
        self.assertEqual(self.store.show_memory(self.store.summarize_conversation(0))['content'], 'gardening roses')

    def test_suppression_and_safe_summary_after_restart(self):
        self.forget()
        self.app.close()
        self.app = Assistant(self.root/'data', self.root/'notes')
        self.store = self.app.store
        with self.assertRaises(ValueError):
            self.automatic('Alpha')
        self.store.message('user', 'gardening roses')
        self.assertEqual(self.store.show_memory(self.store.summarize_conversation(0))['content'], 'gardening roses')

    def test_voice_forget_update_and_summary(self):
        def speak(text):
            return VoiceSession(self.app, MockSTT(text), MockTTS(), MockRecorder(), MockPlayer()).run()
        speak('覚えて Python Alpha')
        key = self.store.memories()[0]['id']
        speak('/memory update ' + str(key) + ' Rust Beta')
        self.assertEqual(self.store.retrieve_memories('Beta')[0]['id'], key)
        speak('Alpha')
        self.assertIsNone(self.store.summarize_conversation(0))
        speak('忘れて ' + str(key))
        speak('gardening roses')
        fresh = self.store.summarize_conversation(0)
        self.assertEqual(self.store.show_memory(fresh)['content'], 'gardening roses')

    def test_external_sources_still_denied_after_resume(self):
        from personal_ai.llm import MockLLM
        self.forget()
        self.app.provider = MockLLM()
        self.app.chat('/web gardening')
        self.app.chat('/calendar')
        self.assertIsNone(self.store.summarize_conversation(0))
        for row in self.store.db.execute('SELECT * FROM conversations').fetchall():
            with self.assertRaises(ValueError):
                self.store.add_memory(row['content'], source='user_conversation', memory_type='fact', confirmed=False,
                                      provenance={'epoch': self.store.epoch(), 'conversation_ids': [row['id']]})
        self.assertIsNotNone(self.automatic('gardening roses'))

    def test_transitive_paraphrase_sources_invalidated(self):
        key = self.app.memory('add', 'Python Alpha')
        assistant = self.store.message('assistant', 'An entirely different phrasing', memory_ids=[key])
        followup = self.store.message('user', 'Yes use that choice', conversation_ids=[assistant])
        derived = self.store.add_memory('Yes use that choice', source='user_conversation', memory_type='fact', confirmed=False,
                                        provenance={'epoch': self.store.epoch(), 'conversation_ids': [followup]})
        self.app.memory('forget', memory_id=key)
        self.assertEqual(self.store.show_memory(derived)['status'], 'stale')
        self.assertEqual(self.store.history(), [])
        self.assertIsNone(self.store.summarize_conversation(0))
        with self.assertRaises(ValueError):
            self.store.add_memory('Yes use that choice', source='user_conversation', memory_type='fact', confirmed=False,
                                  provenance={'epoch': self.store.epoch(), 'conversation_ids': [followup]})

    def test_chat_records_actual_context_dependencies(self):
        key = self.app.memory('add', 'Python Alpha')
        self.app.chat('Python')
        edges = list(self.store.db.execute("SELECT * FROM source_edges WHERE parent_kind='memory' AND parent_id=?", (key,)))
        self.assertTrue(edges)
        self.app.chat('Tell me more')
        self.app.memory('forget', memory_id=key)
        self.assertEqual(self.store.history(), [])

    def test_stale_inflight_write_cannot_revive_sources(self):
        epoch = self.store.epoch()
        self.forget()
        key = self.store.message('user', 'old paraphrase', epoch)
        self.assertEqual(self.store.history(), [])
        with self.assertRaises(ValueError):
            self.store.add_memory('old paraphrase', source='user_conversation', memory_type='fact', confirmed=False,
                                  provenance={'epoch': self.store.epoch(), 'conversation_ids': [key]})

    def test_stale_candidate_rejected_but_safe_source_can_retry(self):
        key = self.store.message('user', 'gardening roses')
        epoch = self.store.epoch()
        self.forget()
        with self.assertRaises(ValueError):
            self.store.add_memory('gardening roses', source='user_conversation', memory_type='fact', confirmed=False,
                                  provenance={'epoch': epoch, 'conversation_ids': [key]})
        self.assertIsNotNone(self.store.summarize_conversation(0))

    def test_tombstones_and_audit_have_no_secret_body(self):
        body = 'password UniqueSensitiveBody'
        key = self.app.memory('add', body)
        self.app.memory('forget', memory_id=key)
        for table in ('memory_blocks', 'suppression_terms', 'source_edges', 'operations', 'memories'):
            rows = [dict(row) for row in self.store.db.execute('SELECT * FROM ' + table)]
            self.assertNotIn('UniqueSensitiveBody', json.dumps(rows))
        self.assertEqual(self.store.show_memory(key)['content'], '')

    def test_unrelated_summary_coverage_survives_epoch_change(self):
        self.store.message('user', 'gardening roses')
        key = self.store.summarize_conversation(0)
        self.forget()
        self.assertEqual(self.store.show_memory(key)['status'], 'active')
        self.assertIsNone(self.store.summarize_conversation(0))

    def test_conflict_rejected_values_stay_suppressed(self):
        key = self.app.memory('add', 'Japanese', claim_key='language')
        other = self.app.memory('add', 'English', claim_key='language')
        self.app.memory('update', 'French', key)
        self.assertEqual(self.store.show_memory(other)['status'], 'superseded')
        for text in ('Japanese', 'English'):
            with self.assertRaises(ValueError):
                self.automatic(text)

    def test_update_detaches_old_revision_dependencies(self):
        key = self.app.memory('add', 'Alpha')
        old = self.store.message('assistant', 'old answer', memory_ids=[key])
        self.app.memory('update', 'Beta', key)
        new = self.store.message('assistant', 'new answer', memory_ids=[key])
        self.assertEqual([r['id'] for r in self.store.history_rows()], [new])
        self.app.memory('forget', memory_id=key)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.db.execute('SELECT status FROM conversations WHERE id=?', (old,)).fetchone()[0], 'stale')

    def test_mutation_rollback_keeps_memory_and_sources(self):
        key = self.automatic('Alpha')
        self.store.db.execute("CREATE TRIGGER fail_memory_update BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT, 'test'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.memory('forget', memory_id=key)
        self.assertEqual(self.store.show_memory(key)['status'], 'active')
        self.assertFalse(self.store.suppressed('Alpha'))
        self.assertEqual(len(self.store.history()), 1)

    def test_known_paraphrase_copies_cannot_revive(self):
        key = self.app.memory('add', 'Alpha')
        self.store.message('assistant', 'entirely different wording', memory_ids=[key])
        copied = self.automatic('entirely different wording')
        self.app.memory('forget', memory_id=key)
        self.assertEqual(self.store.show_memory(copied)['status'], 'stale')
        with self.assertRaises(ValueError):
            self.automatic('repeated: entirely different wording')

    def test_unicode_phrase_embedded_replay_denied(self):
        key = self.app.memory('add', '🔒🦊')
        self.app.memory('forget', memory_id=key)
        with self.assertRaises(ValueError):
            self.automatic('prefix 🔒🦊 suffix')

    def legacy_store(self, prohibition=False, recoverable=True):
        import sqlite3
        from personal_ai.memory import fingerprint
        path = self.root/'legacy41.db'
        db = sqlite3.connect(path)
        db.executescript('''
            CREATE TABLE memory_blocks(fingerprint TEXT PRIMARY KEY);
            CREATE TABLE memory_policy(id INTEGER PRIMARY KEY, automatic_disabled INTEGER NOT NULL);
            INSERT INTO memory_policy VALUES(1,1);
            CREATE TABLE conversations(id INTEGER PRIMARY KEY, epoch INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        db.execute('INSERT INTO memory_blocks VALUES(?)', (fingerprint('Alpha'),))
        if recoverable:
            db.execute("INSERT INTO conversations VALUES(1,0,'user','Alpha','2026-01-01')")
        if prohibition:
            db.execute("INSERT INTO conversations VALUES(2,0,'user','保存禁止','2026-01-01')")
        db.commit()
        db.close()
        return Store(path)

    def test_legacy_barrier_resumes_if_suppression_recoverable(self):
        store = self.legacy_store()
        try:
            self.assertTrue(store.suppressed('Alpha'))
            self.assertEqual(store.db.execute('SELECT automatic_disabled FROM memory_policy').fetchone()[0], 0)
            store.message('user', 'gardening roses')
            key = store.summarize_conversation(0)
            self.assertEqual(store.show_memory(key)['content'], 'gardening roses')
        finally:
            store.close()

    def test_legacy_prohibition_is_not_cleared(self):
        store = self.legacy_store(prohibition=True)
        try:
            self.assertEqual(store.db.execute('SELECT automatic_disabled FROM memory_policy').fetchone()[0], 1)
        finally:
            store.close()

    def test_unrecoverable_legacy_barrier_remains_fail_closed(self):
        store = self.legacy_store(recoverable=False)
        try:
            self.assertEqual(store.db.execute('SELECT automatic_disabled FROM memory_policy').fetchone()[0], 1)
        finally:
            store.close()
