"""Explicit, paused migration of layer labels and dynamics; canonical facts stay byte-identical."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, replace

from .config import FeelingPolicy
from .coordinator import require_coordinator
from .memory_dynamics import DynamicsPolicy, MemoryAnchor, advance, consolidation_level, half_lives
from .memory_lifecycle import TIERS
from .store import Conflict


class MemoryMigration:
    def __init__(self,memory):
        self.memory,self.store=memory,memory.store
        self.store.connection.execute('''CREATE TABLE IF NOT EXISTS memory_schema_origins(
            memory_id TEXT PRIMARY KEY, migration_token TEXT NOT NULL, before_payload TEXT NOT NULL)''')

    def _paused(self):
        cfg,revision=self.store.settings()
        if cfg.runtime_enabled or self.store.rows("SELECT 1 FROM jobs WHERE state='running' LIMIT 1"):
            raise Conflict('memory migration requires a paused bot without running work')
        return cfg,revision

    def _fingerprint(self):
        # Include pending receipts/jobs and graph versions, but not ephemeral logs or the saved preview itself.
        names=[row['name'] for row in self.store.rows("SELECT name FROM sqlite_master WHERE type='table'")
            if (row['name'].startswith('memory_') and not row['name'].startswith('memory_fts'))
            or row['name'] in {'memories','jobs','index_outbox','index_sync','mood_events','mood_sources'}]
        data={name:sorted([tuple(row) for row in self.store.rows(f'SELECT * FROM "{name}"')],key=repr)
              for name in sorted(names)}
        return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,
            default=lambda value:value.hex()).encode()).hexdigest()

    def preview(self,*,now):
        require_coordinator()
        cfg,revision=self._paused()
        if self.memory.schema_version()==2:
            return {'status':'already_migrated','memory_schema_version':2}
        token=uuid.uuid4().hex
        records=[];feelings=[];counts={}
        for row in self.store.rows('SELECT * FROM memories WHERE deleted=0 ORDER BY id'):
            item=self.memory.decode(row)
            if item['tier']=='feeling':
                view=self.memory.with_reading_policy(item,now=now)
                anchor=MemoryAnchor(significance=.5*view['memory_remaining'],initial_significance=.5,
                    detail=view['memory_remaining'],emotion=view['feeling_level'],
                    initial_emotion=view['initial_feeling'],remaining=view['memory_remaining'],
                    since=max(now,view['decay_basis_time']))
                feelings.append({'before':item,'anchor':asdict(anchor),'policy':cfg.memory_policies.feeling.model_dump()})
                counts['feeling']=counts.get('feeling',0)+1
                continue
            if item['tier'] not in TIERS:
                continue
            tier='long_term' if item['tier'] in {'secondary','important'} else item['tier']
            counts[item['tier']]=counts.get(item['tier'],0)+1
            level=self.memory.lifecycle.view(item,cfg.memory_policies,now=now)['memory_remaining']
            existing=self.memory.lifecycle.signals._read(item)
            if existing:
                anchor=MemoryAnchor(**(advance(*existing,now=now)|{'since':max(now,existing[0].since)}))
            else:
                significance=.9 if item['tier']=='important' else .5
                recalls=self.store.rows('SELECT COALESCE(SUM(recall_count),0) FROM memory_merge_recalls WHERE memory_id=?',
                                       (item['id'],))[0][0]
                anchor=MemoryAnchor(significance=significance*level,initial_significance=significance,
                    detail=level,remaining=level,consolidation=consolidation_level(recalls),
                    since=max(now,self.memory.lifecycle.state(item)['since']))
            policy=getattr(cfg.memory_policies,tier).curve()
            override=self.memory.lifecycle.state(item)['half_life_override']
            if tier=='recent' and override is not None:
                policy=replace(policy,base_half_life=max(policy.base_half_life,override*60))
            records.append({'before':item,'tier':tier,'anchor':asdict(anchor),'policy':asdict(policy)})
        plan={'token':token,'status':'prepared','time':now,'config_revision':revision,
              'epoch':self.memory.epoch(),'fingerprint':self._fingerprint(),'records':records,
              'feelings':feelings,'counts':counts}
        self.store.set_state('memory-migration:'+token,plan)
        return {'token':token,'status':'prepared','counts':counts,'memory_schema_version':1,
                'target_version':2,'preserves_content':True,'automatic_reembedding':False,
                'items':[{'memory_id':record['before']['id'],'before_tier':record['before']['tier'],
                    'tier':record['tier'],'initial_significance':record['anchor']['initial_significance'],
                    'current_remaining':record['anchor']['remaining'],
                    'half_life_days':half_lives(MemoryAnchor(**record['anchor']),
                        DynamicsPolicy(**record['policy']))['meaning']/86400}
                    for record in records]}

    def apply(self,token):
        require_coordinator()
        if not isinstance(token,str) or len(token)!=32 or any(char not in '0123456789abcdef' for char in token):
            raise ValueError('invalid migration token')
        plan=self.store.get_state('memory-migration:'+token)
        if not plan:
            raise Conflict('memory migration preview is missing')
        if plan['status']=='applied':
            return plan['receipt']
        _,revision=self._paused()
        if (self.memory.schema_version()!=1 or revision!=plan['config_revision']
                or self.memory.epoch()!=plan['epoch'] or self._fingerprint()!=plan['fingerprint']):
            raise Conflict('memory migration preview changed; prepare it again')
        if self.store.connection.in_transaction:
            raise RuntimeError('migration backup must precede its write transaction')
        backup=self.store.path.parent/'backups'/('memory-v2-'+token+'.db')
        backup.parent.mkdir(exist_ok=True)
        if not backup.exists():
            temporary=backup.with_suffix('.db.tmp')
            with closing(sqlite3.connect(temporary)) as destination:
                self.store.connection.backup(destination)
                destination.execute('PRAGMA journal_mode=DELETE')
            temporary.replace(backup)
        with closing(sqlite3.connect(backup.resolve().as_uri()+'?mode=ro',uri=True)) as check:
            if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise OSError('memory migration backup failed integrity check')
        # Recheck after backup; a concurrent change must not slip between preview and commit.
        with self.store.transaction() as db:
            _,current_revision=self._paused()
            if (current_revision!=plan['config_revision'] or self.memory.epoch()!=plan['epoch']
                    or self._fingerprint()!=plan['fingerprint']):
                raise Conflict('memory changed during migration backup')
            for record in plan['records']:
                before=record['before']
                db.execute('INSERT INTO memory_schema_origins VALUES(?,?,?)',
                           (before['id'],token,json.dumps(before,ensure_ascii=False)))
                # Layer metadata changes, semantic revisions do not: graph/vector evidence remains current.
                db.execute('UPDATE memories SET tier=? WHERE id=?',(record['tier'],before['id']))
                current=self.memory.get(before['id'])
                anchor=MemoryAnchor(**record['anchor']);policy=DynamicsPolicy(**record['policy'])
                if self.memory.lifecycle.signals._read(current):
                    self.memory.lifecycle.signals._write(current,anchor,policy)
                else:
                    self.memory.lifecycle.signals.initialize(current,anchor,policy)
                for receipt in self.store.rows('SELECT * FROM memory_merge_recalls WHERE memory_id=?',(before['id'],)):
                    db.executemany('INSERT OR IGNORE INTO memory_signal_events(memory_id,event_id,time) VALUES(?,?,?)',
                        [(before['id'],f'merge:{receipt["operation_id"]}:{index}',receipt['recalled_at'])
                         for index in range(receipt['recall_count'])])
            for record in plan.get('feelings',[]):
                before=record['before']
                db.execute('INSERT INTO memory_schema_origins VALUES(?,?,?)',
                           (before['id'],token,json.dumps(before,ensure_ascii=False)))
                self.memory.feelings.save(before,MemoryAnchor(**record['anchor']),FeelingPolicy(**record['policy']))
                self.memory.mood.claim_sources(before['source_message_ids'],persona_id=before['persona_id'])
            self.store.set_state('memory_schema_version',2)
            self.store.set_state('memory_epoch',plan['epoch']+1)
            receipt={'token':token,'status':'applied','memory_schema_version':2,
                     'migrated':len(plan['records'])+len(plan.get('feelings',[])),
                     'feeling_migrated':len(plan.get('feelings',[])),'backup':str(backup)}
            self.store.set_state('memory-migration:'+token,plan|{'status':'applied','receipt':receipt})
        return receipt
