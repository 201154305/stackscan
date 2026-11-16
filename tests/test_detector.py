import pytest

from stackscan.detector import Detector, Page


@pytest.fixture(scope="module")
def detector() -> Detector:
    return Detector.default()


def names(detections) -> set[str]:
    return {d.technology for d in detections}


def test_wordpress_version_from_meta_generator(detector):
    page = Page(html='<html><head><meta name="generator" content="WordPress 6.6.2"></head></html>')
    [wp] = [d for d in detector.detect(page) if d.technology == "WordPress"]
    assert wp.version == "6.6.2"
    assert wp.category == "CMS"
    assert wp.evidence == "meta generator: WordPress 6.6.2"


def test_woocommerce_implies_wordpress(detector):
    page = Page(html='<script src="/wp-content/plugins/woocommerce/assets/js/cart.min.js"></script>')
    found = {d.technology: d for d in detector.detect(page)}
    assert "WooCommerce" in found
    assert found["WordPress"].category == "CMS"


def test_implied_technology_is_marked_as_implied(detector):
    page = Page(html='<div id="__nuxt"></div><script src="/_nuxt/entry.js"></script>')
    found = {d.technology: d for d in detector.detect(page)}
    assert found["Vue.js"].evidence == "implied by Nuxt"


def test_headers_cookies_and_versions(detector):
    page = Page(
        headers={"server": "nginx/1.25.3", "x-powered-by": "PHP/8.3.1", "cf-ray": "8a1b-GRU"},
        cookies=["laravel_session"],
    )
    found = {d.technology: d for d in detector.detect(page)}
    assert found["Nginx"].version == "1.25.3"
    assert found["PHP"].version == "8.3.1"
    assert {"Cloudflare", "Laravel"} <= found.keys()


def test_framework_announced_in_x_powered_by(detector):
    found = {d.technology: d for d in detector.detect(Page(headers={"x-powered-by": "Nuxt"}))}
    assert found["Nuxt"].evidence == "header x-powered-by: Nuxt"
    assert found["Vue.js"].evidence == "implied by Nuxt"


def test_header_presence_rule_matches_empty_value(detector):
    assert "Shopify" in names(detector.detect(Page(headers={"x-shopid": ""})))


def test_brazilian_ecommerce_platforms(detector):
    vtex = Page(html='<img src="https://loja.vteximg.com.br/arquivos/logo.png">')
    nuvem = Page(html='<link href="https://acdn.mitiendanube.com/stores/001/theme.css">')
    assert "VTEX" in names(detector.detect(vtex))
    assert "Nuvemshop" in names(detector.detect(nuvem))


def test_jquery_version_from_script_src(detector):
    page = Page(html='<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>')
    [jq] = [d for d in detector.detect(page) if d.technology == "jQuery"]
    assert jq.version == "3.7.1"


def test_plain_page_detects_nothing(detector):
    assert detector.detect(Page(html="<html><body>Olá, mundo</body></html>")) == []


def test_broken_html_does_not_raise(detector):
    page = Page(html='<meta name="generator" content="WordPress"><script src=<<<"')
    assert "WordPress" in names(detector.detect(page))


def test_long_evidence_is_truncated(detector):
    src = "https://cdn.shopify.com/" + "a" * 500 + ".js"
    [shop] = [
        d for d in detector.detect(Page(html=f'<script src="{src}"></script>')) if d.technology == "Shopify"
    ]
    assert len(shop.evidence) <= len("script: ") + 120


def test_unknown_implied_technology_is_rejected():
    with pytest.raises(ValueError, match="Ghost"):
        Detector([{"name": "A", "category": "X", "implies": "Ghost"}])
