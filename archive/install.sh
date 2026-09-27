#!/bin/bash
set -e

echo "IsabelleGym auto install script"
echo "================================"

# color definition
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# check system requirements
check_requirements() {
    echo -e "${BLUE}check system requirements...${NC}"
    
    # check operating system
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        echo -e "${GREEN}operating system: Linux (tested and verified)${NC}"
    fi
    
    # check Java
    if command -v java &> /dev/null; then
        JAVA_VERSION=$(java -version 2>&1 | head -n 1 | cut -d'"' -f2 | cut -d'.' -f1)
        if [[ $JAVA_VERSION -ge 17 ]]; then
            echo -e "${GREEN}Java version: $(java -version 2>&1 | head -n 1)${NC}"
        else
            echo -e "${RED}Java version is too low, need JDK 17+${NC}"
            exit 1
        fi
    else
        echo -e "${RED} Java not found, please install JDK 17+${NC}"
        exit 1
    fi
    
    # check Python
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
        echo -e "${GREEN}Python version: $(python3 --version)${NC}"
    else
        echo -e "${RED}Python3 not found${NC}"
        exit 1
    fi
    
    # check necessary tools
    for tool in git curl wget tar; do
        if command -v $tool &> /dev/null; then
            echo -e "${GREEN}$tool: installed${NC}"
        else
            echo -e "${RED} $tool not found${NC}"
            exit 1
        fi
    done
}

# download and install Isabelle
install_isabelle() {
    echo -e "${BLUE}download and install Isabelle 2025...${NC}"
    
    if [ -d "isabelle" ]; then
        echo -e "${YELLOW}Isabelle directory already exists, skip download${NC}"
        return
    fi
    
    # check if Isabelle compressed package already exists
    if [ -f "Isabelle2025_linux.tar.gz" ]; then
        echo -e "${GREEN}local Isabelle compressed package found${NC}"
    else
        echo -e "${BLUE}download Isabelle 2025...${NC}"
        wget https://isabelle.in.tum.de/dist/Isabelle2025_linux.tar.gz
    fi
    
    echo -e "${BLUE}unzip Isabelle...${NC}"
    tar -xf Isabelle2025_linux.tar.gz
    mv Isabelle2025 isabelle
    
    echo -e "${GREEN}Isabelle installation completed${NC}"
}

# set up Python environment
setup_python_env() {
    echo -e "${BLUE}set up Python environment...${NC}"
    
    if [ -d ".venv" ]; then
        echo -e "${YELLOW}Python virtual environment already exists${NC}"
    else
        echo -e "${BLUE}create Python virtual environment...${NC}"
        python3 -m venv .venv
    fi
    
    echo -e "${BLUE}activate virtual environment...${NC}"
    source .venv/bin/activate
    
    echo -e "${BLUE}upgrade pip...${NC}"
    pip install --upgrade pip
    
    echo -e "${BLUE}install Python dependencies...${NC}"
    pip install -e .[dev]
    
    echo -e "${GREEN}Python environment setup completed${NC}"
}

# initialize Isabelle components
init_isabelle_components() {
    echo -e "${BLUE}initialize Isabelle components...${NC}"
    
    if [ ! -f "repl/Admin/init" ]; then
        echo -e "${RED}init script not found${NC}"
        exit 1
    fi
    
    chmod +x repl/Admin/init
    ./repl/Admin/init
    
    echo -e "${GREEN}Isabelle components initialization completed${NC}"
}

# precompile Scala code
build_scala() {
    echo -e "${BLUE}precompile Scala code...${NC}"
    
    cd repl
    
    if [ ! -f "gradlew" ]; then
        echo -e "${RED}Gradle wrapper not found${NC}"
        exit 1
    fi
    
    chmod +x gradlew
    ./gradlew build
    
    cd ..
    
    echo -e "${GREEN}Scala code precompiled${NC}"
}


# show usage
show_usage() {
    echo -e "${BLUE}usage...${NC}"
    echo "================================"
    echo -e "${GREEN}installation completed${NC}"
    echo ""
    echo "next steps:"
    echo "1. activate Python virtual environment:"
    echo "   source .venv/bin/activate"
    echo ""
    echo "2. run example:"
    echo "   python demo_repl.py"
    echo ""
    echo "3. view documentation:"
    echo "   cat README.md"
    echo ""
    echo -e "${YELLOW}tip: please activate virtual environment before using${NC}"
}

# main function
main() {
    echo -e "${BLUE}start installing IsabelleGym...${NC}"
    echo ""
    
    check_requirements
    echo ""
    
    install_isabelle
    echo ""
    
    setup_python_env
    echo ""
    
    init_isabelle_components
    echo ""
    
    build_scala
    echo ""
    
    show_usage
}

# run main function
main "$@" 