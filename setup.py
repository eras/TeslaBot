from setuptools import setup
from typing import List

def lines(filename: str) -> List[str]:
    with open(filename, "r") as input:
        return input.readlines()

setup(
    name='TeslaBot',
    version='0.1.0',
    author='Erkki Seppälä',
    author_email='erkki.seppala@vincit.fi',
    packages=['teslabot'],
    scripts=[],
    url='https://github.com/eras/TeslaBot/',
    license='LICENSE.MIT',
    description='Tool for interacting with the Tesla vehicles',
    long_description=open('README.md').read(),
    install_requires=lines("requirements.txt"),
    extras_require={
        "matrix": lines("requirements-matrix.txt"),
        "slack": lines("requirements-slack.txt"),
    },
)
