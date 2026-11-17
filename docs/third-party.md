# Third-party components

The locally served API documentation uses Swagger UI 5.17.14 from
https://www.npmjs.com/package/swagger-ui-dist/v/5.17.14, licensed under Apache-2.0.
Its JavaScript, stylesheet, icon, LICENSE and NOTICE are retained in app/static.
Bundling these assets allows the API explorer to render without a CDN connection.

The optional model example downloads HuggingFaceTB/SmolLM2-135M-Instruct at
revision 12fd25f77366fa6b3b4b768ec3050bf629380bac from Hugging Face. The model card
declares Apache-2.0. Model weights are not included in this repository. The
example disables remote model code and requires no hosted inference service.

The default v0.3 example uses Qwen/Qwen2.5-1.5B-Instruct at revision
989aa7980e4cf806f80c7fef2b1adb7bc71aa306, also Apache-2.0. The retained smaller
comparison is Qwen/Qwen2.5-0.5B-Instruct at revision
7ae557604adf67be50417f59c2c2f167def9a775. All models load pinned safetensors;
weights are downloaded separately, never committed here.

Python dependencies remain under their respective upstream licences. See
pyproject.toml for direct dependencies. Repository code is covered by LICENSE.
