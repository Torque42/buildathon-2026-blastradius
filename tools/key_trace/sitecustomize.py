import os
if 'BR_KEY_TEST' in os.environ:
    import atexit,gzip,json
    from pathlib import Path
    import keytrace
    keytrace.install()
    keytrace.active=os.environ['BR_KEY_TEST']
    @atexit.register
    def save():
        data={k:{(t or ''):list(s) for t,s in keytrace.value_reads[k].items()} for k in sorted(keytrace.created)}
        (Path(os.environ['BR_KEY_OUT'])/f'{os.getpid()}.json.gz').write_bytes(gzip.compress(json.dumps(data,separators=(',',':')).encode(),mtime=0))
