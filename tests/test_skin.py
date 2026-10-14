import pytest
from PIL import Image

from render.beatmap import ParseBeatmap
from render.catch import BANANA, FRUIT, BuildCatchBeatmap, CatcherTrack, JudgeReplay
from render.replay import CatchFrame, ReplayMods
from render.skin import FRUIT_NAMES, CatcherSprite, LoadSkin, PieceSprite, SkinTexture, Tint, VisibleBox
from render.video import SCALE, FrameRenderer
from tests.skin_fixture import MakeTestSkin
from tests.test_catch import MakeMap


@pytest.fixture
def skin(tmp_path):
    return LoadSkin(MakeTestSkin(str(tmp_path / "skin")))


def test_loads_at_2x_with_half_display_size(skin):
    texture = skin.Texture("fruit-pear")
    assert texture.density == 2 and texture.display_size == (128, 128)


def test_file_names_are_case_insensitive(skin):
    assert skin.Texture("fruit-drop").display_size == (64, 64)


def test_catcher_uses_animation_frame_zero(skin):
    assert skin.Texture("fruit-catcher-idle", animated_first=True) is not None


def test_missing_skin_or_fruits_falls_back(tmp_path):
    assert LoadSkin("") is None
    assert LoadSkin(str(tmp_path / "nowhere")) is None
    (tmp_path / "partial").mkdir()
    Image.new("RGBA", (10, 10)).save(tmp_path / "partial" / "fruit-pear.png")
    assert LoadSkin(str(tmp_path / "partial")) is None


def test_oversized_skin_images_are_skipped(tmp_path, monkeypatch):
    import render.skin as skin_module
    monkeypatch.setattr(skin_module, "MAX_SKIN_PIXELS", 100)
    assert LoadSkin(MakeTestSkin(str(tmp_path / "skin"))) is None


def test_tint_multiplies_colour_and_keeps_alpha():
    image = Image.new("RGBA", (2, 2), (255, 128, 0, 77))
    pixel = Tint(image, (100, 200, 255)).getpixel((0, 0))
    assert pixel == (100, 100, 0, 77)


def test_overlay_is_not_tinted():
    base = SkinTexture(Image.new("RGBA", (10, 10), (255, 255, 255, 255)), 1)
    overlay = SkinTexture(Image.new("RGBA", (4, 4), (255, 255, 255, 255)), 1)
    sprite = PieceSprite(base, overlay, (255, 0, 0), 1)
    assert sprite.getpixel((5, 5))[:3] == (255, 255, 255)
    assert sprite.getpixel((0, 0))[:3] == (255, 0, 0)


def test_hyper_glow_sits_behind_and_is_larger():
    base = SkinTexture(Image.new("RGBA", (10, 10), (255, 255, 255, 255)), 1)
    sprite = PieceSprite(base, None, (0, 0, 255), 1, hyper_colour=(255, 0, 0))
    assert sprite.size == (12, 12)
    assert sprite.getpixel((0, 0))[0] > 0 and sprite.getpixel((6, 6))[:3] == (0, 0, 255)


def test_catcher_visible_width_matches_judging_width(skin):
    image, lift = CatcherSprite(skin.Texture("fruit-catcher-idle", animated_first=True), 150)
    left, _, right, _ = VisibleBox(image)
    assert right - left == pytest.approx(150, abs=2)
    assert lift == round(16 * 2 * 150 / 532)


def test_fruit_types_cycle_by_parent_index_and_streams_share_one(skin):
    beatmap = MakeMap(["100,192,1000,1,0", "100,192,1500,2,0,L|300:192,1,200", "100,192,4000,1,0", "100,192,4500,1,0",
                       "100,192,5000,1,0"])
    catch_beatmap = BuildCatchBeatmap(beatmap, ReplayMods())
    fruits = [o for o in catch_beatmap.objects if o.kind == FRUIT]
    assert [o.index_in_beatmap for o in fruits] == [0, 1, 1, 2, 3, 4]
    assert {o.index_in_beatmap for o in catch_beatmap.objects if o.kind != FRUIT} == {1}


def test_skinned_renderer_draws_every_fruit_type(skin):
    beatmap = MakeMap(["100,192,1000,1,0", "200,192,1300,1,0", "300,192,1600,1,0", "400,192,1900,1,0"])
    catch_beatmap = BuildCatchBeatmap(beatmap, ReplayMods())
    frames = [CatchFrame(t, 100 + t * 0.15, True) for t in range(0, 2600, 16)]
    track = CatcherTrack(frames)
    replay = type("Replay", (), {"frames": frames, "mods": ReplayMods(), "username": "Tester"})()
    renderer = FrameRenderer(beatmap, catch_beatmap, replay, track, JudgeReplay(catch_beatmap, track), skin=skin)
    assert {key[3] for key in renderer.sprites if key[0] == FRUIT} == set(range(len(FRUIT_NAMES)))
    assert (BANANA, 0, False, 0) in renderer.sprites
    assert renderer.RenderFrame(60).size == (1280, 720)
    left, _, right, _ = VisibleBox(renderer.catcher_sprites[(False, 1)])
    assert right - left == pytest.approx(renderer.catch.catch_width * SCALE, abs=2)
    assert renderer.catcher_lift > 0
