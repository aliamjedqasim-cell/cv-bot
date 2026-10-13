# Read by gunicorn automatically (it looks for gunicorn.conf.py in the start folder).
# Background jobs (webhook registration, keep-awake, watchdog) start inside each worker after the fork,
# so the parent process never opens a network connection that the worker could inherit half-done.


def post_fork(server, worker):
    import app
    app.start_background()
