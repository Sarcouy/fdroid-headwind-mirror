-- Headwind n'hebergera jamais les APK: les appareils les telechargent depuis le depot F-Droid.
-- La colonne reservait un mode miroir qui ne sera pas implemente.
ALTER TABLE tracked_package DROP COLUMN mirror;
