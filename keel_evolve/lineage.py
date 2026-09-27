"""Pinned skill ancestry and transitive withdrawal; no executable code in lineage."""
from contextlib import contextmanager
import hashlib
import json
from collections import deque

from keel_machine.common import canonical, clone, ident, require, sha
from .interpreter import validate_rule

MAX_PARENTS = 8
MAX_ANCESTORS = 64


class Lineage:
    @staticmethod
    def _parents(body):
        require(type(body) is dict,'lineage_body_invalid')
        rows=body.get('dependencies',[])
        require(type(rows) is list and len(rows)<=MAX_PARENTS,'lineage_parent_limit')
        require(all(type(row) is dict and set(row)=={'artifact_id','body_sha256'} for row in rows),'lineage_binding_invalid')
        for row in rows:
            ident(row['artifact_id']);sha(row['body_sha256'])
        require(len({row['artifact_id'] for row in rows})==len(rows),'lineage_duplicate_parent')
        return rows

    def _validate_dependencies(self, db, body, artifact_id):
        pending=[(artifact_id, body, ())]
        visited=set()
        while pending:
            current, document, path=pending.pop()
            require(current not in path,'lineage_cycle')
            if current in visited:continue
            visited.add(current)
            # The candidate itself is not one of its ancestors: enforce the
            # documented MAX_ANCESTORS boundary on the ancestor set alone so
            # exactly MAX_ANCESTORS ancestors are accepted.
            require(len(visited-{artifact_id})<=MAX_ANCESTORS,'lineage_ancestor_limit')
            for link in self._parents(document):
                require(link['artifact_id']!=artifact_id,'lineage_cycle')
                row,_=super()._active(db,link['artifact_id'],('SIMULATION','PROMOTED'))
                require(row['body_sha256']==link['body_sha256'],'lineage_parent_changed')
                pending.append((row['artifact_id'],json.loads(row['body_json']),path+(current,)))
        return visited-{artifact_id}

    def _active(self, db, artifact_id, states=('PROMOTED',)):
        row, life=super()._active(db,artifact_id,states)
        self._validate_dependencies(db,json.loads(row['body_json']),artifact_id)
        if row['state']=='PROMOTED':self._promotion_ready(db,row)
        return row, life

    def _promotion_ready(self, db, row):
        ancestors=self._validate_dependencies(db,json.loads(row['body_json']),row['artifact_id'])
        for ancestor in ancestors:
            super()._active(db,ancestor,('PROMOTED',))

    def derive(self, artifact_id, source_artifact_id, evidence_ids, *, additional_parents=(),
               rule=None, ttl_seconds=3600):
        """Propose an immutable descendant; every parent must currently be usable.

        Copies an existing schema, optionally changes its closed rule, and binds
        all direct parents by ID and body hash. The result always starts CANDIDATE.
        """
        ident(artifact_id);ident(source_artifact_id)
        require(type(additional_parents) in (list,tuple) and len(additional_parents)<MAX_PARENTS,'lineage_parent_limit')
        parents=[source_artifact_id,*additional_parents]
        require(len(set(parents))==len(parents),'lineage_duplicate_parent')
        if rule is not None:validate_rule(rule)
        with self._transaction() as db:
            source,_=self._active(db,source_artifact_id,('SIMULATION','PROMOTED'))
            body=json.loads(source['body_json'])
            links=[]
            for parent in parents:
                ident(parent)
                row,_=self._active(db,parent,('SIMULATION','PROMOTED'))
                require(row['task_family']==source['task_family'],'lineage_family_mismatch')
                require(row['kind']==source['kind'],'lineage_kind_mismatch')
                inherited=json.loads(row['body_json'])
                if source['kind']=='LESSON':
                    for key,value in inherited['applicability'].items():
                        require(key not in body['applicability'] or canonical(body['applicability'][key])==canonical(value),
                                'lineage_applicability_conflict')
                        body['applicability'][key]=value
                    body['invalidators']=sorted(set(body['invalidators'])|set(inherited['invalidators']))
                    require(len(body['applicability'])<=64 and len(body['invalidators'])<=64,'lineage_context_limit')
                else:
                    require(canonical(body['required_state'])==canonical(inherited['required_state'])
                            and set(body['mutable_inputs'])==set(inherited['mutable_inputs']), 'lineage_precondition_conflict')
                    body['validators']=sorted(set(body['validators'])|set(inherited['validators']))
                    require(len(body['validators'])<=64,'lineage_validator_limit')
                links.append({'artifact_id':parent,'body_sha256':row['body_sha256']})
            body['dependencies']=sorted(links,key=lambda x:x['artifact_id'])
            body['ttl_seconds']=self._ttl(ttl_seconds)
            if rule is not None:body['rule']=clone(rule)
        # _propose rechecks every pin/lifecycle in the transaction that inserts.
        return self._propose(artifact_id,source['kind'],source['task_family'],body,evidence_ids)

    def qualify(self, evaluation_id, artifact_id, *args, **kwargs):
        with self._transaction() as db:
            row,_=self._active(db,artifact_id,('CANDIDATE','SIMULATION','PROMOTED'))
            ancestors=self._validate_dependencies(db,json.loads(row['body_json']),artifact_id)
            for ancestor in ancestors:
                super()._active(db,ancestor,('PROMOTED',))
        return super().qualify(evaluation_id,artifact_id,*args,**kwargs)

    def lineage(self, artifact_id):
        ident(artifact_id)
        with self._transaction() as db:
            row=db.execute('SELECT body_json,state FROM artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
            require(row is not None,'artifact_missing')
            return {'artifact_id':artifact_id,'state':row['state'],
                    'parents':self._parents(json.loads(row['body_json'])),'execution_authorized':False}

    def _cascade(self, db):
        # The surrounding storage quota bounds this snapshot to 4,096 artifacts.
        rows=db.execute('SELECT a.*,l.expires_at FROM artifacts a LEFT JOIN evolution_lifecycle l USING(artifact_id)').fetchall()
        now=self._clock(db)
        unavailable=set();children={};bad_links=set();invalid_roots=set()
        by_id={row['artifact_id']:row for row in rows}
        for row in rows:
            if row['state'] not in ('SIMULATION','PROMOTED') or row['expires_at'] is None or row['expires_at']<=now:
                unavailable.add(row['artifact_id'])
                if row['state'] in ('SIMULATION','PROMOTED'):invalid_roots.add(row['artifact_id'])
            try:
                require(hashlib.sha256(row['body_json']).hexdigest()==row['body_sha256'],'artifact_body_corrupt')
                for link in self._parents(json.loads(row['body_json'])):
                    children.setdefault(link['artifact_id'],[]).append(row['artifact_id'])
                    parent=by_id.get(link['artifact_id'])
                    if parent is None or parent['body_sha256']!=link['body_sha256']:
                        bad_links.add(row['artifact_id'])
            except (ValueError,TypeError,KeyError):
                unavailable.add(row['artifact_id'])
                if row['state'] in ('SIMULATION','PROMOTED'):invalid_roots.add(row['artifact_id'])
        queue=deque(unavailable|bad_links);held=set(bad_links|invalid_roots)
        while queue:
            for child in children.get(queue.popleft(),[]):
                if child not in held:
                    held.add(child);queue.append(child)
        for child in held:
            # Record the cascade as the hold event for requalification evidence.
            db.execute("UPDATE artifacts SET state='HELD',reason=NULL,updated_at=? WHERE artifact_id=? AND state!='RETIRED'",(now,child,))
        return held

    @contextmanager
    def _transaction(self):
        with super()._transaction() as db:
            self._cascade(db)
            yield db
            self._cascade(db)
