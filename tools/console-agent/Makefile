# n64to3ds console agent: builds build/n64to3ds-agent.{cia,3dsx}.
# Needs the devkitPro environment that scripts/build.sh sets (DEVKITPRO, DEVKITARM, PATH with
# makerom, smdhtool and 3dsxtool).  Icon and banner are borrowed from the
# pinned Daedalus checkout so that no binary assets are kept here.

ifeq ($(strip $(DEVKITARM)),)
$(error DEVKITARM is not set; export the toolchain variables as scripts/build.sh does)
endif

TARGET    := n64to3ds-agent
BUILD     := build
CTRULIB   ?= $(DEVKITPRO)/libctru
RESOURCES ?= $(CURDIR)/../../third_party/daedalus/Source/SysCTR/Resources

CC      := $(DEVKITARM)/bin/arm-none-eabi-gcc
ARCH    := -march=armv6k -mtune=mpcore -mfloat-abi=hard -mtp=soft
CFLAGS  := -g -Wall -Wextra -O2 -mword-relocations -ffunction-sections $(ARCH) -D__3DS__ -I$(CTRULIB)/include
LDFLAGS := -specs=3dsx.specs -g $(ARCH) -L$(CTRULIB)/lib
LIBS    := -lctru -lm

ELF  := $(BUILD)/$(TARGET).elf
SMDH := $(BUILD)/$(TARGET).smdh

.PHONY: all clean
all: $(BUILD)/$(TARGET).cia $(BUILD)/$(TARGET).3dsx

$(BUILD):
	@mkdir -p $@

$(ELF): source/main.c | $(BUILD)
	$(CC) $(CFLAGS) $< $(LDFLAGS) $(LIBS) -o $@

$(SMDH): $(RESOURCES)/icon.png | $(BUILD)
	smdhtool --create "n64to3ds agent" "Install and launch n64to3ds builds over Wi-Fi" "n64to3ds" $< $@

$(BUILD)/$(TARGET).3dsx: $(ELF) $(SMDH)
	3dsxtool $(ELF) $@ --smdh=$(SMDH)

$(BUILD)/$(TARGET).cia: $(ELF) $(SMDH) agent.rsf $(RESOURCES)/banner.bnr
	makerom -f cia -target t -exefslogo -o $@ -elf $(ELF) -rsf agent.rsf \
		-banner $(RESOURCES)/banner.bnr -icon $(SMDH)

clean:
	rm -rf $(BUILD)
