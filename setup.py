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
        "matrix-nio[e2e]==0.19.0",
    ],
)
