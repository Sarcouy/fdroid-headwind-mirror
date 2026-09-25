-- Headwind will never host the APKs: the devices download them from the F-Droid repository.
-- The column was reserved for a mirror mode that will not be implemented.
ALTER TABLE tracked_package DROP COLUMN mirror;
