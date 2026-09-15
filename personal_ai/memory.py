"""Local, conservative memory policy. No model or external tool can write memories."""
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone

TYPES = {'explicit_memory', 'user_preference', 'fact', 'project_context',
         'temporary_context', 'conversation_summary'}
TYPE_WEIGHT = {'explicit_memory': 1, 'user_preference': .9, 'fact': .6,
               'project_context': .7, 'temporary_context': .8, 'conversation_summary': .3}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def fingerprint(text):
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def tokens(text):
    text = normalize(text)
    words = set(re.findall(r'[a-z0-9_]+', text))
    for run in re.findall(r'[\u3040-\u30ff\u3400-\u9fff]+', text):
        words.update(run[i:i+2] for i in range(len(run)-1))
        if len(run) == 1:
            words.add(run)
    return words


def private(text):
    return bool(re.search(
        r'(?i)secret|password|passwd|api[ _-]?key|\btoken\b|private[ _-]?key|'
        r'パスワード|秘密鍵|トークン|保存(?:しない|禁止|しないで)|覚えないで|'
        r'do not (?:store|remember)|don.t (?:store|remember)|'
        r'-----BEGIN|\bsk-[\w-]+|\bgh[pousr]_[\w]+|\bAKIA[A-Z0-9]+|'
        r'\beyJ[\w-]+\.[\w-]+\.[\w-]+', text))


def date(value):
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError('memory_expiry_invalid') from None
    if result.tzinfo is None:
        raise ValueError('memory_timezone_required')
    return result.astimezone(timezone.utc)


class MemoryStore:
    def init_memory(self):
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(memories)')}
        additions = {'type': "TEXT NOT NULL DEFAULT 'explicit_memory'",
                     'last_accessed_at': 'TEXT', 'confidence': 'REAL NOT NULL DEFAULT 1',
                     'importance': 'REAL NOT NULL DEFAULT 0.5', 'expires_at': 'TEXT',
                     'status': "TEXT NOT NULL DEFAULT 'active'",
                     'provenance': "TEXT NOT NULL DEFAULT '{}'",
                     'confirmed': 'INTEGER NOT NULL DEFAULT 1',
                     'claim_key': 'TEXT', 'fingerprint': "TEXT NOT NULL DEFAULT ''"}
        with self.db:
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute('ALTER TABLE memories ADD COLUMN ' + name + ' ' + definition)
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS memory_blocks (fingerprint TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS memory_conflicts (
                    id INTEGER PRIMARY KEY, left_id INTEGER NOT NULL, right_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                    UNIQUE(left_id,right_id));
                CREATE TABLE IF NOT EXISTS memory_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), automatic_disabled INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO memory_policy VALUES(1,0);
                CREATE INDEX IF NOT EXISTS memory_status ON memories(status,expires_at);
            ''')
            for row in self.db.execute("SELECT id,content FROM memories WHERE fingerprint='' ").fetchall():
                self.db.execute('UPDATE memories SET fingerprint=? WHERE id=?',
                                (fingerprint(row['content']), row['id']))
                self.db.execute("UPDATE memories SET provenance=? WHERE id=?", (json.dumps({'origin': 'legacy_explicit_user_command', 'conversation_ids': [], 'epoch': self.epoch()}), row['id']))
        self.last_retrieval = []

    def _audit_memory(self, action, metadata):
        # Called within the mutation transaction. Never log content, query or claim key.
        self.db.execute('''INSERT INTO operations(name,status,metadata,created_at,finished_at)
                           VALUES (?,?,?,?,?)''',
                        ('memory_' + action, 'success', json.dumps(metadata), stamp(), stamp()))

    def expire_memories(self):
        with self.db:
            rows = self.db.execute("SELECT id FROM memories WHERE status IN ('active','conflict') AND expires_at<=?",
                                   (stamp(),)).fetchall()
            for row in rows:
                self.db.execute("UPDATE memories SET status='expired',updated_at=? WHERE id=?", (stamp(), row['id']))
                self._audit_memory('expire', {'memory_id': row['id']})

    def memories(self):
        self.expire_memories()
        return [dict(r) for r in self.db.execute("SELECT * FROM memories WHERE status IN ('active','conflict') ORDER BY id")]

    def show_memory(self, memory_id):
        self.expire_memories()
        row = self.db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
        if row is None:
            raise ValueError('memory_not_found')
        result = dict(row)
        result['provenance'] = json.loads(result['provenance'])
        result['conflicts'] = [dict(r) for r in self.db.execute(
            'SELECT * FROM memory_conflicts WHERE left_id=? OR right_id=?', (memory_id, memory_id))]
        return result

    def add_memory(self, content, *, memory_type='explicit_memory', source='explicit_user_command',
                   confirmed=True, confidence=1., importance=.5, expires_at=None,
                   provenance=None, claim_key=None):
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError('memory_content_invalid')
        if memory_type not in TYPES or source not in ('explicit_user_command', 'user_conversation', 'conversation_summary'):
            raise ValueError('memory_source_or_type_denied')
        explicit = source == 'explicit_user_command'
        if not explicit and (confirmed or memory_type == 'explicit_memory'):
            raise ValueError('memory_confirmation_required')
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
               for v in (confidence, importance)):
            raise ValueError('memory_score_invalid')
        if expires_at is not None:
            expires_at = date(expires_at).isoformat()
        if memory_type == 'temporary_context' and expires_at is None:
            raise ValueError('memory_expiry_required')
        provenance = dict(provenance or {})
        # Only numeric source references are persisted; arbitrary metadata could contain secrets.
        refs = provenance.get('conversation_ids', [])
        if not isinstance(refs, list) or any(type(i) is not int for i in refs):
            raise ValueError('memory_provenance_invalid')
        content = content.strip()
        fp = fingerprint(content)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not explicit:
                if private(content) or self.db.execute('SELECT automatic_disabled FROM memory_policy WHERE id=1').fetchone()[0]:
                    raise ValueError('automatic_memory_denied')
                if not refs or provenance.get('epoch') != self.epoch():
                    raise ValueError('memory_provenance_invalid')
                for ref in refs:
                    row = self.db.execute('SELECT * FROM conversations WHERE id=?', (ref,)).fetchone()
                    if row is None or row['epoch'] != self.epoch() or row['role'] != 'user' or private(row['content']):
                        raise ValueError('memory_provenance_denied')
                # Extractive only: no invented facts or instruction-following LLM summaries.
                originals = [self.db.execute('SELECT content FROM conversations WHERE id=?', (ref,)).fetchone()[0] for ref in refs]
                if content != '\n'.join(originals).strip():
                    raise ValueError('memory_extraction_denied')
            if not explicit and self.db.execute('SELECT 1 FROM memory_blocks WHERE fingerprint=?', (fp,)).fetchone():
                raise ValueError('forgotten_memory_denied')
            existing = self.db.execute("SELECT * FROM memories WHERE fingerprint=? AND status IN ('active','conflict')", (fp,)).fetchone()
            if existing:
                if explicit and not existing['confirmed']:
                    self.db.execute('UPDATE memories SET confirmed=1,source=?,type=?,updated_at=? WHERE id=?',
                                    (source, memory_type, stamp(), existing['id']))
                    self._audit_memory('update', {'memory_id': existing['id'], 'reason': 'user_confirmed'})
                return existing['id']
            trace = {'epoch': self.epoch(), 'conversation_ids': refs, 'origin': source}
            cursor = self.db.execute('''INSERT INTO memories
                (content,source,created_at,updated_at,type,confidence,importance,expires_at,status,provenance,confirmed,claim_key,fingerprint)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (content, source, stamp(), stamp(), memory_type, confidence, importance, expires_at,
                 'active', json.dumps(trace), int(explicit), claim_key, fp))
            key = cursor.lastrowid
            self._audit_memory('add', {'memory_id': key, 'type': memory_type})
            if claim_key:
                for other in self.db.execute("SELECT id FROM memories WHERE claim_key=? AND id<>? AND status IN ('active','conflict')", (claim_key, key)).fetchall():
                    self.db.execute("UPDATE memories SET status='conflict' WHERE id IN (?,?)", (key, other['id']))
                    self.db.execute('INSERT OR IGNORE INTO memory_conflicts(left_id,right_id,created_at) VALUES(?,?,?)', (other['id'], key, stamp()))
                    self._audit_memory('conflict', {'memory_ids': [other['id'], key], 'confirmation_required': True})
            return key

    def memory(self, action, content=None, memory_id=None):
        if action == 'add':
            return self.add_memory(content)
        if action not in ('update', 'forget'):
            raise ValueError('memory_action_invalid')
        if action == 'update' and (not isinstance(content, str) or not content.strip() or len(content) > 4000):
            raise ValueError('memory_content_invalid')
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute("SELECT * FROM memories WHERE status<>'forgotten'" +
                                   ('' if memory_id == 'all' and action == 'forget' else ' AND id=?'),
                                   () if memory_id == 'all' and action == 'forget' else (memory_id,)).fetchall()
            if not rows and memory_id != 'all':
                raise ValueError('memory_not_found')
            for row in rows:
                if action == 'update':
                    # An explicit edit of a conflicted claim selects that value.
                    peers = self.db.execute("SELECT left_id,right_id FROM memory_conflicts WHERE status='pending' AND (left_id=? OR right_id=?)", (row['id'], row['id'])).fetchall()
                    for peer in peers:
                        other = peer['right_id'] if peer['left_id'] == row['id'] else peer['left_id']
                        self.db.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?", (stamp(), other))
                self.db.execute('INSERT OR IGNORE INTO memory_blocks VALUES(?)', (row['fingerprint'],))
                if action == 'forget':
                    # Remove exact duplicates regardless of status. Preserve only non-content tombstones.
                    self.db.execute("UPDATE memories SET content='',provenance='{}',claim_key=NULL,status='forgotten',updated_at=? WHERE fingerprint=?", (stamp(), row['fingerprint']))
                else:
                    self.db.execute("UPDATE memories SET content=?,fingerprint=?,confirmed=1,source='explicit_user_command',status='active',updated_at=?,provenance=? WHERE id=?",
                                    (content.strip(), fingerprint(content), stamp(), json.dumps({'epoch': self.epoch()+1, 'origin': 'explicit_user_command', 'conversation_ids': []}), memory_id))
                self.db.execute("UPDATE memory_conflicts SET status='resolved_by_user' WHERE left_id=? OR right_id=?", (row['id'], row['id']))
            # Conservative privacy barrier: all derived memory and old conversations are invalidated.
            # Disable automatic ingestion after a correction/forget, including paraphrases and stale summaries.
            self.db.execute("UPDATE memories SET content='',provenance='{}',claim_key=NULL,status='forgotten',updated_at=? WHERE confirmed=0 OR type='conversation_summary'", (stamp(),))
            self.db.execute("UPDATE memory_conflicts SET status='resolved_by_user' WHERE status='pending' AND (left_id IN (SELECT id FROM memories WHERE status IN ('forgotten','superseded')) OR right_id IN (SELECT id FROM memories WHERE status IN ('forgotten','superseded')))")
            self.db.execute('UPDATE memory_policy SET automatic_disabled=1 WHERE id=1')
            self.db.execute('UPDATE state SET epoch=epoch+1 WHERE id=1')
            self.db.execute("UPDATE memories SET status='active' WHERE status='conflict' AND id NOT IN (SELECT left_id FROM memory_conflicts WHERE status='pending' UNION SELECT right_id FROM memory_conflicts WHERE status='pending')")
            self._audit_memory(action, {'memory_id': memory_id, 'derived_invalidated': True})
            self.last_retrieval = []
        return memory_id

    def retrieve_memories(self, query, limit=6):
        if not isinstance(query, str) or not query.strip() or len(query) > 16000 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('memory_query_invalid')
        terms = tokens(query)
        inspect = query.strip() in ('記憶を教えて', '覚えていることは？', '確認')
        candidates = []
        available = self.memories()
        if not available:
            self.last_retrieval = []
            return []
        for row in available:
            overlap = terms & tokens(row['content'])
            relevance = len(overlap) / max(1, len(terms))
            if not overlap and not inspect:
                continue
            age = max(0., (datetime.now(timezone.utc) - date(row['updated_at'])).total_seconds()/86400)
            recency = 1 / (1 + age/30)
            components = {'relevance': relevance, 'importance': row['importance'], 'recency': recency,
                          'type': TYPE_WEIGHT[row['type']], 'confirmed': row['confirmed']}
            score = 5*relevance + row['importance'] + .5*recency + .5*components['type'] + 2*row['confirmed']
            reason = {'memory_id': row['id'], 'score': round(score, 6), 'components': components,
                      'overlap_count': len(overlap), 'reason': 'inspection' if inspect else 'lexical',
                      'confirmation_required': row['status'] == 'conflict'}
            candidates.append((score, row, reason))
        candidates.sort(key=lambda item: (-item[1]['confirmed'], -item[0], item[1]['id']))
        selected, reasons, seen = [], [], set()
        for _, row, reason in candidates:
            if row['fingerprint'] in seen:
                continue
            seen.add(row['fingerprint'])
            # Conflict values are withheld until the user explicitly resolves them.
            if row['status'] == 'conflict':
                reason['withheld'] = True
                reasons.append(reason)
                continue
            if len(selected) < limit and sum(len(r['content']) for r in selected) + len(row['content']) <= 8000:
                row['retrieval_reason'] = reason
                selected.append(row)
                reasons.append(reason)
        with self.db:
            for row in selected:
                self.db.execute('UPDATE memories SET last_accessed_at=? WHERE id=?', (stamp(), row['id']))
            self._audit_memory('retrieve', {'selected_ids': [r['id'] for r in selected], 'reasons': reasons})
        self.last_retrieval = reasons
        return selected

    def summarize_conversation(self, keep_recent=12):
        """Bounded extractive summary of old, safe user turns; no tool/assistant data."""
        if type(keep_recent) is not int or keep_recent < 0:
            raise ValueError('summary_limit_invalid')
        rows = self.db.execute("SELECT * FROM conversations WHERE epoch=? AND role IN ('user','assistant') ORDER BY id DESC", (self.epoch(),)).fetchall()
        covered = set()
        for memory in self.db.execute("SELECT provenance FROM memories WHERE type='conversation_summary' AND status='active'"):
            trace = json.loads(memory['provenance'])
            if trace.get('epoch') == self.epoch():
                covered.update(trace.get('conversation_ids', []))
        old = [row for row in reversed(rows[keep_recent:]) if row['id'] not in covered]
        safe, size = [], 0
        for row in old:
            if row['role'] != 'user' or private(row['content']) or row['content'].startswith('/'):
                continue
            if size + len(row['content']) + 1 > 4000:
                continue
            safe.append(row)
            size += len(row['content']) + 1
        if not safe:
            return None
        return self.add_memory('\n'.join(r['content'] for r in safe), memory_type='conversation_summary',
                               source='conversation_summary', confirmed=False, confidence=.5,
                               provenance={'epoch': self.epoch(), 'conversation_ids': [r['id'] for r in safe]})
