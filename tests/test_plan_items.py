from unittest.mock import AsyncMock, patch

from custom_components.ica_shopping.const import DATA_ICA, DOMAIN

from .test_list_dinners import FakeList, _call, iso, setup  # noqa: F401


async def _commit(hass, **data):
    with patch("custom_components.ica_shopping._pick_calendar", return_value=None):
        return await _call(hass, "plan_dinners", recipes="1,2", **data)


async def test_only_the_chosen_items_go_on_and_come_off_again(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)

    response = await _commit(hass, start_date=iso(1), items=["pasta", "lök"])

    assert response["added"] == ["400 g pasta", "1 gul lök", "2 rödlökar"]
    assert the_list.texts() == response["added"]
    pasta, soppa = response["dinners"]
    assert pasta["put_on_list"] == ["400 g pasta", "1 gul lök"]
    assert soppa["put_on_list"] == ["2 rödlökar"]
    assert "items_not_found" not in response
    # Left off this time only
    assert hass.data[DOMAIN]["current_options"] == {}

    response = await _call(hass, "remove_dinner", date=iso(1))
    assert response["list_removed"] == ["400 g pasta", "1 gul lök"]
    assert the_list.texts() == ["2 rödlökar"]
    assert memory.on(iso(2))["put_on_list"] == ["2 rödlökar"]


async def test_a_summed_row_is_both_dinners(hass, setup):
    the_list = FakeList()
    await setup(the_list)

    response = await _commit(hass, start_date=iso(1), items=["Vispgrädde"])

    assert the_list.texts() == ["4.5 dl vispgrädde"]
    assert [d["put_on_list"] for d in response["dinners"]] == [["4.5 dl vispgrädde"]] * 2

    # Soppa still needs it
    assert (await _call(hass, "remove_dinner", date=iso(1)))["list_removed"] == []
    assert (await _call(hass, "remove_dinner", date=iso(2)))["list_removed"] == ["4.5 dl vispgrädde"]
    assert the_list.texts() == []


async def test_dinners_planned_earlier_keep_their_day_and_get_the_rows(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _commit(hass, start_date=iso(3), add_to_list=False)
    assert [(e["date"], e["list"]) for e in memory.history] == [(iso(3), None), (iso(4), None)]

    # Shopping later, without a start date
    response = await _commit(hass, items=["pasta"])

    assert [d["date"] for d in response["dinners"]] == [iso(3), iso(4)]
    assert len(memory.history) == 2
    assert memory.on(iso(3))["put_on_list"] == ["400 g pasta"]
    assert memory.on(iso(3))["list"] == "Handla"
    assert "put_on_list" not in memory.on(iso(4))

    await _call(hass, "remove_dinner", date=iso(3))
    assert the_list.texts() == []


async def test_tonights_dinner_is_not_planned_twice(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)
    await _commit(hass, start_date=iso(0), add_to_list=False)

    response = await _commit(hass, items=["vispgrädde"])

    assert [d["date"] for d in response["dinners"]] == [iso(0), iso(1)]
    assert len(memory.history) == 2


async def test_no_items_adds_nothing_and_unknown_names_are_reported(hass, setup):
    the_list = FakeList()
    memory = await setup(the_list)

    response = await _commit(hass, start_date=iso(1), items=[])
    assert response["added"] == [] and the_list.texts() == []
    assert len(memory.history) == 2

    response = await _commit(hass, start_date=iso(1), items=["pasta", "kanel"])
    assert response["added"] == ["400 g pasta"]
    assert response["items_not_found"] == ["kanel"]


async def test_a_chosen_item_goes_on_as_its_product(hass, setup):
    the_list = FakeList()
    await setup(the_list)
    api = hass.data[DOMAIN][DATA_ICA]
    api.async_get_favorite_stores = AsyncMock(return_value=[{"id": "s1", "name": "ICA Nära"}])
    api.async_get_store_account_number = AsyncMock(return_value="1003380")
    product = {
        "ingredient": "vispgrädde", "needed": "4.5 dl vispgrädde",
        "text": "Vispgrädde 36% 5dl (4.5 dl vispgrädde)",
    }
    match = AsyncMock(return_value=[product])

    with patch("custom_components.ica_shopping.recipes.async_match_products", match):
        response = await _commit(hass, start_date=iso(1), match_products=True, items=["vispgrädde"])

    # Only the chosen ingredients are looked up, not the pasta
    assert [i["name"] for i in match.await_args.args[1]] == ["vispgrädde"]
    assert response["added"] == [product["text"]]
    assert [d["put_on_list"] for d in response["dinners"]] == [[product["text"]]] * 2


async def test_a_preview_says_which_rows_cover_each_ingredient(hass, setup):
    the_list = FakeList("Pasta", "3 dl vispgrädde", "Gul lök", "Ost")
    await setup(the_list)

    response = await _call(hass, "plan_dinners", recipes="1,2", preview=True)

    groups = {g["name"]: g.get("on_list") for g in response["ingredient_groups"]}
    # "3 dl" doesn't cover the 4.5 dl the plan needs, and the red onions aren't a gul lök
    assert groups == {"vispgrädde": None, "pasta": "Pasta", "Lök": None}
    assert the_list.texts() == ["Pasta", "3 dl vispgrädde", "Gul lök", "Ost"]


async def test_a_struck_row_does_not_count_as_on_the_list(hass, setup):
    the_list = FakeList("Pasta")
    the_list.rows[0]["isStriked"] = True
    await setup(the_list)

    response = await _call(hass, "plan_dinners", recipes="1,2", preview=True)

    assert all("on_list" not in g for g in response["ingredient_groups"])
