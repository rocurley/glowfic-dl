# Glowflow Reader 🌟

A better Glowfic reading experience, built on top of [Streamlit](https://docs.streamlit.io/). [Try it here!](https://share.streamlit.io/akrolsmir/glowflow/main)

Also, you can specify a post! https://share.streamlit.io/akrolsmir/glowflow/main?post=5111

## Developing locally

Install dependencies via [Pipenv](https://docs.streamlit.io/library/get-started/installation#install-pipenv), then:

```
streamlit run streamlit_app.py
```

## TODOs

- [x] Cache the posts on disk, so we don't have to fetch each time
- [x] Route `http://glowflow.io` to this page?
- [x] Support dark mode?
- [ ] Trim down unneeded stuff from the ebook side of things
  - [ ] OR combine back with ebook reader code, to add a one-click "download as Ebook" feature
- [ ] Support boards and continuities??
