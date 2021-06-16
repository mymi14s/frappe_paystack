from setuptools import setup, find_packages

with open('requirements.txt') as f:
	install_requires = f.read().strip().split('\n')

# get version from __version__ variable in frappe_paystack/__init__.py
from frappe_paystack import __version__ as version

setup(
	name='frappe_paystack',
	version=version,
	description='Paystack payment gateway for Frappe and ERPext',
	author='Anthony Emmanuel (Ghorz.com)',
	author_email='mymi14s@gmail.com',
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
