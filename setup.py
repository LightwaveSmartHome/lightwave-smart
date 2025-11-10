import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="lightwave_smart",
    version="2.0.1",
    author="Bryan Blunt / Lightwave",
    author_email="dev@lightwaverf.com",
    description="Controls for Lightwave Smart Series (second generation) devices",
    install_requires=[
        "aiohttp<=4",
    ],
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/LightwaveSmartHome/lightwave_smart",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
