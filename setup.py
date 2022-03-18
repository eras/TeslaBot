from setuptools import setup

setup(
    name='TeslaBot',
    version='0.1.0',
    author='Erkki Seppälä',
    author_email='erkki.seppala@vincit.fi',
    packages=['teslabot'],
    scripts=[],
    url='http://pypi.python.org/pypi/teslabot/',
    license='LICENSE.MIT',
    description='Tool for interacting with the Tesla vehicles',
    long_description=open('README.md').read(),
    install_requires=[
        "TeslaPy==2.4.0",
        "python-socketio[asyncio_client]==5.5.2",
        "aiohttp==3.8.1",
        "aiounittest==1.4.1",
    ],
    extras_require={
        "matrix": [
            "matrix-nio[e2e]==0.19.0",
        ],
        "slack": [
            "slackclient==2.9.3",
        ],
    },
)
