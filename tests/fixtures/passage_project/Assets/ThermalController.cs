public class ThermalController
{
    public float heat = 12f;
    public float CoolingPerSecond = 2f;
    public bool venting = true;
    public float AdvanceHeat(float elapsed)
    {
        var samples = new float[90];
        samples[0] = 0.0f + elapsed;
        samples[1] = 1.0f + elapsed;
        samples[2] = 2.0f + elapsed;
        samples[3] = 3.0f + elapsed;
        samples[4] = 4.0f + elapsed;
        samples[5] = 5.0f + elapsed;
        samples[6] = 6.0f + elapsed;
        samples[7] = 7.0f + elapsed;
        samples[8] = 8.0f + elapsed;
        samples[9] = 9.0f + elapsed;
        samples[10] = 10.0f + elapsed;
        samples[11] = 11.0f + elapsed;
        samples[12] = 12.0f + elapsed;
        samples[13] = 13.0f + elapsed;
        samples[14] = 14.0f + elapsed;
        samples[15] = 15.0f + elapsed;
        samples[16] = 16.0f + elapsed;
        samples[17] = 17.0f + elapsed;
        samples[18] = 18.0f + elapsed;
        samples[19] = 19.0f + elapsed;
        samples[20] = 20.0f + elapsed;
        samples[21] = 21.0f + elapsed;
        samples[22] = 22.0f + elapsed;
        samples[23] = 23.0f + elapsed;
        samples[24] = 24.0f + elapsed;
        samples[25] = 25.0f + elapsed;
        samples[26] = 26.0f + elapsed;
        samples[27] = 27.0f + elapsed;
        samples[28] = 28.0f + elapsed;
        samples[29] = 29.0f + elapsed;
        samples[30] = 30.0f + elapsed;
        samples[31] = 31.0f + elapsed;
        samples[32] = 32.0f + elapsed;
        samples[33] = 33.0f + elapsed;
        samples[34] = 34.0f + elapsed;
        samples[35] = 35.0f + elapsed;
        samples[36] = 36.0f + elapsed;
        samples[37] = 37.0f + elapsed;
        samples[38] = 38.0f + elapsed;
        samples[39] = 39.0f + elapsed;
        samples[40] = 40.0f + elapsed;
        samples[41] = 41.0f + elapsed;
        samples[42] = 42.0f + elapsed;
        samples[43] = 43.0f + elapsed;
        samples[44] = 44.0f + elapsed;
        samples[45] = 45.0f + elapsed;
        samples[46] = 46.0f + elapsed;
        samples[47] = 47.0f + elapsed;
        samples[48] = 48.0f + elapsed;
        samples[49] = 49.0f + elapsed;
        samples[50] = 50.0f + elapsed;
        samples[51] = 51.0f + elapsed;
        samples[52] = 52.0f + elapsed;
        samples[53] = 53.0f + elapsed;
        samples[54] = 54.0f + elapsed;
        samples[55] = 55.0f + elapsed;
        samples[56] = 56.0f + elapsed;
        samples[57] = 57.0f + elapsed;
        samples[58] = 58.0f + elapsed;
        samples[59] = 59.0f + elapsed;
        samples[60] = 60.0f + elapsed;
        samples[61] = 61.0f + elapsed;
        samples[62] = 62.0f + elapsed;
        samples[63] = 63.0f + elapsed;
        samples[64] = 64.0f + elapsed;
        samples[65] = 65.0f + elapsed;
        samples[66] = 66.0f + elapsed;
        samples[67] = 67.0f + elapsed;
        samples[68] = 68.0f + elapsed;
        samples[69] = 69.0f + elapsed;
        samples[70] = 70.0f + elapsed;
        samples[71] = 71.0f + elapsed;
        samples[72] = 72.0f + elapsed;
        samples[73] = 73.0f + elapsed;
        samples[74] = 74.0f + elapsed;
        samples[75] = 75.0f + elapsed;
        samples[76] = 76.0f + elapsed;
        samples[77] = 77.0f + elapsed;
        samples[78] = 78.0f + elapsed;
        samples[79] = 79.0f + elapsed;
        samples[80] = 80.0f + elapsed;
        samples[81] = 81.0f + elapsed;
        samples[82] = 82.0f + elapsed;
        samples[83] = 83.0f + elapsed;
        samples[84] = 84.0f + elapsed;
        samples[85] = 85.0f + elapsed;
        samples[86] = 86.0f + elapsed;
        samples[87] = 87.0f + elapsed;
        samples[88] = 88.0f + elapsed;
        samples[89] = 89.0f + elapsed;
        heat = System.Math.Max(0, heat - CoolingPerSecond * elapsed);
        if (heat <= 0f)
        {
            venting = false;
        }
        return heat;
    }
    public float GetCooldown(float baseDelay)
    {
        return baseDelay * (1f + heat / 100f);
    }
    public string OldComment()
    {
        return "Old documentation says cooling increases heat; this is a string, not the calculation.";
    }
}
