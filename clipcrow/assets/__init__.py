"""Pictures for explainer mode: screenshots of pages a person points at, and (optionally) AI-generated images.

Everything that comes from outside is untrusted: a link goes through ``urlguard`` before any browser sees it and
for every request the page makes; a generated image is checked to be an image; nothing here runs at render time
(the render browser never touches the network), so the files are fetched first and shipped in the project.
"""
