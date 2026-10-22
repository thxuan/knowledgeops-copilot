"""Account sessions use opaque cookies; passwords are Argon2id hashes, never recoverable."""
import hashlib
import json
import secrets
import time
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from app.core.errors import AppError
from app.core.identity import Identity
from app.db.postgres import connection

HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
COOKIE = 'knowledgeops_session'

class AccountRepository:
    def __init__(self, database_url):
        self.database_url = database_url

    def connection(self):
        return connection(self.database_url)

    def initialize(self):
        with self.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(52003004)')
            db.execute('''CREATE TABLE IF NOT EXISTS ko_accounts (
                user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
                display_name TEXT NOT NULL, password_hash TEXT NOT NULL, audiences TEXT NOT NULL,
                ticket_capabilities TEXT NOT NULL, platform_capabilities TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT true, failures INTEGER NOT NULL DEFAULT 0,
                locked_until DOUBLE PRECISION NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL);
                CREATE TABLE IF NOT EXISTS ko_login_sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES ko_accounts,
                csrf_token TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL);
                CREATE TABLE IF NOT EXISTS ko_login_rates (
                bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at DOUBLE PRECISION NOT NULL);
                CREATE TABLE IF NOT EXISTS ko_account_audit (
                event_id BIGSERIAL PRIMARY KEY, actor TEXT NOT NULL, target TEXT NOT NULL,
                action TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
            ''')

    @staticmethod
    def identity(row):
        return Identity(row['user_id'], row['tenant_id'], frozenset(json.loads(row['audiences'])),
            frozenset(json.loads(row['ticket_capabilities'])), frozenset(json.loads(row['platform_capabilities'])))

    @staticmethod
    def public(row):
        return {k:row[k] for k in ('user_id','username','tenant_id','display_name','enabled')}

    def user(self, user_id):
        with self.connection() as db:
            row=db.execute('SELECT * FROM ko_accounts WHERE user_id=%s AND enabled=true',(user_id,)).fetchone()
            if row is None:
                raise AppError('UNAUTHENTICATED','账号不可用，请重新登录。',401)
            return self.identity(row)

    def create(self, username, password, display_name, identity, *, actor='bootstrap'):
        if not 12 <= len(password) <= 256:
            raise AppError('WEAK_PASSWORD','密码长度须为 12–256 个字符。',422)
        hashed=HASHER.hash(password)
        with self.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(52003005)')
            if db.execute('SELECT 1 FROM ko_accounts WHERE user_id=%s OR username=%s',(identity.user_id,username.lower())).fetchone():
                raise AppError('ACCOUNT_EXISTS','账号或用户标识已存在。',409)
            db.execute('''INSERT INTO ko_accounts
                (user_id,username,tenant_id,display_name,password_hash,audiences,ticket_capabilities,platform_capabilities,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(identity.user_id,username.lower(),identity.tenant_id,display_name,hashed,
                json.dumps(sorted(identity.audiences)),json.dumps(sorted(identity.ticket_capabilities)),json.dumps(sorted(identity.platform_capabilities)),time.time()))
            self.audit(db,actor,identity.user_id,'created')
        return {'user_id':identity.user_id,'username':username.lower()}

    @staticmethod
    def audit(db,actor,target,action):
        db.execute('INSERT INTO ko_account_audit(actor,target,action,created_at) VALUES (%s,%s,%s,%s)',(actor,target,action,time.time()))

    def login(self, username, password, client_ip, lifetime):
        now=time.time();username=username.lower()
        bucket=hashlib.sha256(client_ip.encode()).hexdigest()
        # Persist IP and account throttling across processes. Do not hold DB locks during Argon2.
        with self.connection() as db:
            db.execute('''INSERT INTO ko_login_rates VALUES (%s,1,%s) ON CONFLICT(bucket) DO UPDATE
                SET attempts=CASE WHEN ko_login_rates.reset_at<%s THEN 1 ELSE ko_login_rates.attempts+1 END,
                    reset_at=CASE WHEN ko_login_rates.reset_at<%s THEN excluded.reset_at ELSE ko_login_rates.reset_at END''',(bucket,now+300,now,now))
            limited=db.execute('SELECT attempts FROM ko_login_rates WHERE bucket=%s',(bucket,)).fetchone()['attempts']>30
            row=db.execute('SELECT * FROM ko_accounts WHERE username=%s',(username,)).fetchone()
        if limited:
            raise AppError('LOGIN_RATE_LIMIT','登录尝试过于频繁，请稍后再试。',429)
        if row and row['locked_until']>now:
            raise AppError('LOGIN_FAILED','用户名或密码错误，或账号暂不可用。',401)
        valid=False
        try:
            valid=HASHER.verify(row['password_hash'] if row else DUMMY_HASH,password)
        except (VerificationError,InvalidHashError):
            pass
        with self.connection() as db:
            current=db.execute('SELECT * FROM ko_accounts WHERE username=%s FOR UPDATE',(username,)).fetchone()
            if not current or not valid or not current['enabled'] or current['password_hash']!=row['password_hash'] or current['locked_until']>now:
                if current:
                    db.execute('UPDATE ko_accounts SET failures=failures+1,locked_until=CASE WHEN failures>=4 THEN %s ELSE locked_until END WHERE user_id=%s',(now+300,current['user_id']))
                failure=True
            else:
                failure=False
                hashed=HASHER.hash(password) if HASHER.check_needs_rehash(current['password_hash']) else current['password_hash']
                db.execute('UPDATE ko_accounts SET failures=0,locked_until=0,password_hash=%s WHERE user_id=%s',(hashed,current['user_id']))
                token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
                db.execute('DELETE FROM ko_login_sessions WHERE expires_at<%s',(now,))
                db.execute('INSERT INTO ko_login_sessions VALUES (%s,%s,%s,%s,%s)',(hashlib.sha256(token.encode()).hexdigest(),current['user_id'],csrf,now+lifetime,now))
                self.audit(db,current['user_id'],current['user_id'],'login')
        # Raise after commit so failed attempts are not rolled back.
        if failure:
            raise AppError('LOGIN_FAILED','用户名或密码错误，或账号暂不可用。',401)
        return token,csrf,self.public(current)

    def session(self,token):
        if not token or len(token)>128:
            raise AppError('UNAUTHENTICATED','请先登录。',401)
        with self.connection() as db:
            row=db.execute('''SELECT a.*,s.csrf_token FROM ko_login_sessions s JOIN ko_accounts a USING(user_id)
                WHERE s.token_hash=%s AND s.expires_at>%s AND a.enabled=true''',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
            if row is None:
                raise AppError('UNAUTHENTICATED','登录已过期或账号已停用，请重新登录。',401)
            return self.identity(row),row['csrf_token'],self.public(row)

    def logout(self,token):
        if token:
            with self.connection() as db:
                db.execute('DELETE FROM ko_login_sessions WHERE token_hash=%s',(hashlib.sha256(token.encode()).hexdigest(),))

    def set_enabled(self,user_id,enabled,actor):
        with self.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(52003005)')
            row=db.execute('SELECT * FROM ko_accounts WHERE user_id=%s AND tenant_id=%s',(user_id,actor.tenant_id)).fetchone()
            if row is None:
                raise AppError('NOT_FOUND','账号不存在。',404)
            if not enabled and (user_id==actor.user_id or ('manage_platform' in json.loads(row['platform_capabilities']) and db.execute("SELECT count(*) AS n FROM ko_accounts WHERE tenant_id=%s AND enabled=true AND platform_capabilities::jsonb ? 'manage_platform'",(actor.tenant_id,)).fetchone()['n']<=1)):
                raise AppError('LAST_ADMIN','不能停用自己或最后一位平台管理员。',409)
            db.execute('UPDATE ko_accounts SET enabled=%s WHERE user_id=%s',(enabled,user_id))
            if not enabled:
                db.execute('DELETE FROM ko_login_sessions WHERE user_id=%s',(user_id,))
            self.audit(db,actor.user_id,user_id,'enabled' if enabled else 'disabled')
            return {'user_id':user_id,'enabled':enabled}
