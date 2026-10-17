import os
import uvicorn
from app import config

if __name__ == '__main__':
    uvicorn.run('app.main:app', host=os.getenv('HOST', '127.0.0.1'), port=int(os.getenv('PORT', '8765')),
                proxy_headers=True, forwarded_allow_ips='127.0.0.1')
