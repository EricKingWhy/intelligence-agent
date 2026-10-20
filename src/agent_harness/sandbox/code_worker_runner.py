"""Standalone code executed inside the restricted Docker computation container."""

import ast
import asyncio
import contextlib
import inspect
import json
import sys


def main():
    incoming = sys.stdin
    outgoing = sys.stdout

    def send(value):
        outgoing.write(json.dumps(value, ensure_ascii=True) + '\n')
        outgoing.flush()

    request = json.loads(incoming.readline())
    remaining = request['output_chars']
    truncated = False

    class Output:
        def write(self, value):
            nonlocal remaining, truncated
            text = str(value)
            accepted = text[:remaining]
            remaining -= len(accepted)
            truncated = truncated or len(text) > len(accepted)
            for offset in range(0, len(accepted), 8192):
                send({'type': 'output', 'text': accepted[offset:offset + 8192]})
            return len(text)

        def flush(self):
            pass

    class Tools:
        def __getattr__(self, name):
            async def invoke(arguments=None, **kwargs):
                if arguments is not None and kwargs:
                    raise TypeError('Pass an argument object OR keyword arguments')
                args = kwargs if arguments is None else arguments
                if not isinstance(args, dict):
                    raise TypeError('Tool arguments must be an object')
                send({'type': 'call', 'name': name, 'args': args})
                response = json.loads(incoming.readline())
                if response.get('type') != 'result':
                    raise RuntimeError('Invalid tool bridge response')
                return response['result']
            return invoke

        __getitem__ = __getattr__

    namespace = {'tools': Tools(), '__name__': '__main__'}
    try:
        with contextlib.redirect_stdout(Output()), contextlib.redirect_stderr(Output()):
            compiled = compile(
                request['code'], '<exec>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )
            value = eval(compiled, namespace)
            if inspect.isawaitable(value):
                asyncio.run(value)
            if 'result' in namespace:
                print(json.dumps(namespace['result'], ensure_ascii=False, default=str))
        send({'type': 'done', 'ok': True, 'truncated': truncated})
    except BaseException as error:  # noqa: BLE001 -- report script SystemExit/KeyboardInterrupt too
        send({'type': 'done', 'ok': False, 'error': (
            type(error).__name__ + ': ' + str(error)
        )[:2000], 'truncated': truncated})


if __name__ == '__main__':
    main()
