"""Real-browser coverage for the field-distribution workflow."""

from urllib.parse import urlsplit

from playwright.sync_api import Page, expect


def test_field_walk_supports_one_operator_and_multiple_participants(app_page: Page):
    page = app_page

    # Open the new field control and bootstrap the first (admin) account.
    page.locator('[data-action="field"]').click()
    drawer = page.locator(".field-drawer")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Set up the first account")

    drawer.locator('[data-role="auth-name"]').fill("Tom")
    drawer.locator('[data-role="auth-user"]').fill("tom")
    drawer.locator('[data-role="auth-pass"]').fill("correct-horse")
    drawer.locator('[data-action="bootstrap"]').click()
    expect(drawer).to_contain_text("Signed in as")
    expect(drawer).to_contain_text("Tom")

    # One active campaign, then a helper who is selectable but does not need a login.
    drawer.locator('[data-role="new-campaign"]').fill("Thai for £20")
    drawer.locator('[data-action="new-campaign"]').click()
    expect(drawer.locator('[data-role="campaign"]')).to_have_value(
        page.locator('[data-role="campaign"] option', has_text="Thai for £20").get_attribute("value")
    )

    drawer.locator("details").evaluate("(el) => el.open = true")
    drawer.locator('[data-role="person-name"]').fill("Chloe")
    drawer.locator('[data-action="add-person"]').click()
    expect(drawer.locator(".field-people")).to_contain_text("Chloe")

    # Authentication identity and walk participants are deliberately separate.
    tom = drawer.locator('.field-check', has_text="Tom").locator('input[data-role="participant"]')
    chloe = drawer.locator('.field-check', has_text="Chloe").locator('input[data-role="participant"]')
    if not tom.is_checked():
        tom.check()
    chloe.check()
    expect(tom).to_be_checked()
    expect(chloe).to_be_checked()

    # Give the real browser a location and accept the start/finish dialogs.
    parsed = urlsplit(page.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    page.context.grant_permissions(["geolocation"], origin=origin)
    page.context.set_geolocation({"latitude": 52.6070, "longitude": 1.7290})

    def dialogs(dialog):
        if dialog.type == "prompt":
            dialog.accept("42")
        else:
            dialog.accept()

    page.on("dialog", dialogs)

    start = drawer.locator('[data-action="start"]')
    expect(start).to_be_enabled()
    start.click()

    live = page.locator(".field-live")
    expect(live).to_be_visible()
    expect(live).to_contain_text("Tom + Chloe")
    expect(live).to_contain_text("Thai for £20")

    # watchPosition receives an initial fix; moving the mocked device exercises
    # local trace append without depending on real runner GPS.
    page.context.set_geolocation({"latitude": 52.6072, "longitude": 1.7292})
    expect(live.locator("small")).to_contain_text("points")

    live.locator('[data-action="pause"]').click()
    expect(live.locator('[data-action="resume"]')).to_be_visible()
    live.locator('[data-action="resume"]').click()
    expect(live.locator('[data-action="pause"]')).to_be_visible()

    # Finish keeps the local record until the server has acknowledged all
    # points/status/derived coverage, then refreshes the shared campaign view.
    live.locator('[data-action="finish"]').click()
    expect(live).to_be_hidden()
    page.locator('[data-action="field"]').click()
    expect(drawer).to_be_visible()
    expect(drawer.locator(".field-metrics")).to_contain_text("1")
    expect(drawer.locator(".field-history-list")).to_contain_text("Tom + Chloe")
