from conftest import pg_dsn
from app.db.ingestion import IngestionRepository
from app.db.snapshots import initialize_core, evaluation_snapshot
from app.db.conversations import ConversationRepository
from app.core.identity import LOCAL_IDENTITIES
from psycopg import sql
import json

def test_evaluation_snapshot_isolates_history_and_preserves_generated_ids(tmp_path):
    url = pg_dsn()
    source = IngestionRepository(url)
    initialize_core(source)
    identity = LOCAL_IDENTITIES['u_engineer_001']
    conversation = ConversationRepository(source).load(None, identity)
    response = {'answer': 'first', 'citations': [], 'claims': []}
    ConversationRepository(source).commit(conversation, identity, 'q', response, [], 2048)
    copy, schema = evaluation_snapshot(url, tmp_path)
    try:
        snapshot = ConversationRepository(copy).load(conversation['conversation_id'], identity)
        ConversationRepository(copy).commit(snapshot, identity, 'q2', response, [], 2048)
        original = ConversationRepository(source).read(conversation['conversation_id'], identity)
        cloned = ConversationRepository(copy).read(conversation['conversation_id'], identity)
        assert len(original['messages']) == 2 and original['revision'] == 1
        assert len(cloned['messages']) == 4 and cloned['revision'] == 2
        assert cloned['messages'][-1]['message_id'] > original['messages'][-1]['message_id']
        assert json.loads((tmp_path/'snapshot.json').read_text(encoding='utf-8'))['format'] == 'knowledgeops-core-v1'
    finally:
        with source.connection() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
