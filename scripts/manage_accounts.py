"""Create an administrator/account locally; secrets only enter via a hidden terminal prompt."""
import argparse
from getpass import getpass
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.core.config import Settings
from app.core.identity import Identity
from app.db.accounts import AccountRepository,HASHER

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url')
    parser.add_argument('--username',required=True)
    parser.add_argument('--user-id',default='u_admin_001')
    parser.add_argument('--tenant-id',default='tenant_clearwater')
    parser.add_argument('--display-name',default='平台管理员')
    parser.add_argument('--ensure-admin',action='store_true',help='Skip prompting if this enabled administrator already exists')
    parser.add_argument('--reset-password',action='store_true',help='Reset an existing password and revoke all sessions')
    args=parser.parse_args()
    settings=Settings.from_env();repo=AccountRepository(args.database_url or settings.auth_database_url or settings.database_url)
    repo.initialize()
    if args.ensure_admin:
        import json
        with repo.connection() as db:
            existing=db.execute('SELECT * FROM ko_accounts WHERE username=%s',(args.username.lower(),)).fetchone()
        if existing:
            if not existing['enabled'] or 'manage_platform' not in json.loads(existing['platform_capabilities']):
                parser.error('Existing account is disabled or is not an administrator; no privileges were changed')
            print('Administrator already exists; using the existing password.')
            raise SystemExit(0)
    password=getpass('Password (12–256 characters): ')
    if not 12<=len(password)<=256 or password!=getpass('Confirm password: '):
        parser.error('Password length or confirmation invalid')
    if args.reset_password:
        with repo.connection() as db:
            user=db.execute('SELECT user_id FROM ko_accounts WHERE username=%s',(args.username.lower(),)).fetchone()
            if not user:parser.error('Account not found')
            db.execute('UPDATE ko_accounts SET password_hash=%s,failures=0,locked_until=0 WHERE user_id=%s',(HASHER.hash(password),user['user_id']))
            db.execute('DELETE FROM ko_login_sessions WHERE user_id=%s',(user['user_id'],))
            repo.audit(db,'local_operator',user['user_id'],'password_reset')
    else:
        repo.create(args.username,password,args.display_name,
                    Identity(args.user_id,args.tenant_id,frozenset(),frozenset(),frozenset({'manage_platform'})))
    print('Account ready. No password or session token was written to logs.')
