"""Сверка видит, что бэкап ядра встал (P0-2, P0-5).

Молчащий крон выглядит ровно как отсутствующий, поэтому пустые носители и
протухший архив обязаны отличаться друг от друга и от нормы."""
import os, sys, time, shutil, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import contextd_reconcile as rc


class БэкапЯдра(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.цель = os.path.join(self.tmp, "backup")
        os.makedirs(self.цель)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def архив(self, дней_назад):
        p = os.path.join(self.цель, "core-2026-09-0%d.tar.gz.gpg" % (дней_назад + 1))
        open(p, "wb").write(b"gpg")
        t = time.time() - дней_назад * 86400
        os.utime(p, (t, t))
        return p

    def test_свежий_архив_не_находка(self):
        self.архив(0)
        self.assertEqual(rc.бэкап_ядра([self.цель]), [])

    def test_протухший_архив_это_ошибка(self):
        self.архив(5)
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual((f[0]["check"], f[0]["level"]), ("бэкап-ядра-устарел", "error"))
        self.assertEqual(f[0]["days"], 5.0)

    def test_свежий_на_одном_носителе_закрывает_вопрос(self):
        второй = os.path.join(self.tmp, "backup2")
        os.makedirs(второй)
        self.архив(0)
        self.assertEqual(rc.бэкап_ядра([self.цель, второй]), [],
                         "отключённая шара — не повод кричать, копия есть")

    def test_пустые_носители_это_ненастроенность_а_не_отказ(self):
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual((f[0]["check"], f[0]["level"]), ("бэкап-ядра-нет", "warn"))

    def test_несмонтированный_носитель_виден_отдельно(self):
        f = rc.бэкап_ядра([os.path.join(self.tmp, "нет-такого")])
        self.assertEqual(f[0]["check"], "бэкап-ядра-носители")


if __name__ == "__main__":
    unittest.main()
